"""Repo inventory, sync board, preflight/handoff, intermission queue, vault, replication."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..gpu_and_media import discover_node_models
from ..inventory_sync import (
    compare_repo_inventories,
    enqueue_intermission_event,
    list_intermission_queue,
    list_vault_files,
    pull_local_repo_ff,
    read_repo_inventory,
    read_tool_inventory,
    resolve_vault_path,
    sync_local_tool_inventory,
    upsert_peer_repos,
    upsert_peer_tools,
)
from ..repo_ops import find_repo
from ..repo_tracker import (
    hold_counts,
    is_repo_held,
    read_three_way_status,
    set_repo_hold,
)
from ..security import path_allowed, read_roots
from ..tools_loader import load_python_tools
from ._common import zone


class RepoSyncBody(BaseModel):
    repoName: str
    action: str = "pull"
    targetNode: str = "both"
    agent: str = "agy-cli"
    chatId: str = ""


class GitPullBody(BaseModel):
    repoName: str


class HoldSetBody(BaseModel):
    repoName: str
    state: str = "active"
    note: str = ""


class PreflightBody(BaseModel):
    repo: str = ""
    cwd: str = ""
    node: str = ""
    source: str = "agent"


class HandoffBody(BaseModel):
    repo: str
    toNode: str = ""
    fromNode: str = ""


class IntermissionEnqueueBody(BaseModel):
    category: str
    target: str
    targetNode: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class VaultWriteBody(BaseModel):
    relPath: str
    content: str = ""
    contentBase64: str = ""


class VaultCopyBody(BaseModel):
    sourcePath: str
    sourceNode: str = "local"
    destRelPath: str = ""


JUNK_THRESHOLD = 200


def _recommend(status: str, states: dict[str, dict[str, Any]], active: dict[str, Any] | None) -> dict[str, Any]:
    """Plain-language next step for a repo row: which machine has the newer work, and where it should go."""
    if status in ("in_sync", "single_node"):
        return {"action": "none", "text": "Nothing to do" if status == "in_sync" else "Only on one machine"}
    changed = {n: v["dirtyCount"] + v["untrackedCount"] for n, v in states.items()}
    junk = [n for n, c in changed.items() if c >= JUNK_THRESHOLD]
    if junk:
        return {"action": "gitignore", "node": junk[0],
                "text": f"{changed[junk[0]]} changed files on {junk[0]}: probably build output or downloads. Add them to .gitignore before syncing."}
    dirty = [n for n, c in changed.items() if c]
    ahead = [n for n, v in states.items() if v["ahead"]]
    if len(dirty) > 1:
        return {"action": "review", "text": f"Unfinished changes on {' and '.join(dirty)}. Pick the machine you're working on; the other side's work is kept safe."}
    source = (dirty or ahead or [None])[0]
    if source:
        target = next((n for n in states if n != source), None)
        if active and active.get("nodeId") and active["nodeId"] != source:
            target = active["nodeId"]
        if target:
            return {"action": "handoff", "from": source, "to": target, "text": f"{source} has newer work. Bring it to {target}?"}
    if status == "branch_mismatch":
        return {"action": "review", "text": "The machines are on different branches."}
    return {"action": "review", "text": "Commits differ between machines; pull or hand off from the one you used last."}


def build(ctx) -> APIRouter:
    r = APIRouter()

    async def _refresh_peer_inventory(refresh: bool) -> dict[str, bool]:
        online = {}
        for pid in ctx.nodes.peer_ids():
            try:
                data = await ctx.nodes.json(pid, "GET", "/api/repos/inventory", params={"localOnly": "true", "refresh": str(refresh).lower()}, timeout=30.0)
                upsert_peer_repos(ctx.conn, pid, data.get("localRepos", []))
                online[pid] = True
            except Exception:
                online[pid] = False
        return online

    @r.get("/api/repos/inventory")
    async def inventory(localOnly: bool = False, refresh: bool = False) -> dict[str, Any]:
        local = await ctx.rescan_repos() if refresh or not read_repo_inventory(ctx.conn, ctx.nodes.local_id) else read_repo_inventory(ctx.conn, ctx.nodes.local_id)
        if localOnly:
            return {"nodeId": ctx.nodes.local_id, "localRepos": local}
        online = await _refresh_peer_inventory(refresh)
        return {"nodeId": ctx.nodes.local_id, "peersOnline": online, "desktopOnline": any(online.values()), "localRepos": local,
                "comparison": compare_repo_inventories(read_repo_inventory(ctx.conn)), "queue": list_intermission_queue(ctx.conn)}

    @r.get("/api/repos/board")
    async def board(refresh: bool = False) -> dict[str, Any]:
        """One row per repo: state on each node + the active device + recent handoffs."""
        if refresh:
            await ctx.rescan_repos()
        online = await _refresh_peer_inventory(refresh)
        activity = ctx.repo_sync.activity()
        rows = read_repo_inventory(ctx.conn)
        by_repo: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = by_repo.setdefault(row["repoName"].lower(), {"repo": row["repoName"], "nodes": {}})
            entry["nodes"][row["nodeId"]] = {k: row[k] for k in ("branch", "headSha", "ahead", "behind", "dirtyCount", "untrackedCount", "lastCommitAt", "branches")}
        out = []
        for key, entry in sorted(by_repo.items()):
            states = entry["nodes"]
            heads = {v["headSha"] for v in states.values()}
            branches = {v["branch"] for v in states.values()}
            dirty = [n for n, v in states.items() if v["dirtyCount"] or v["untrackedCount"]]
            status = ("single_node" if len(states) < 2 else "dirty" if dirty else "branch_mismatch" if len(branches) > 1
                      else "in_sync" if len(heads) == 1 else "different_commits")
            out.append({**entry, "status": status, "dirtyOn": dirty, "active": activity.get(key),
                        "recommendation": _recommend(status, states, activity.get(key))})
        return {"nodeId": ctx.nodes.local_id, "peersOnline": online, "repos": out, "handoffs": ctx.repo_sync.recent_handoffs(30)}

    @r.get("/api/repos/tracker")
    async def repo_tracker(refresh: bool = False) -> dict[str, Any]:
        """Three-way repo status (laptop, desktop, GitHub remote) sorted by lastChangeAt descending."""
        if refresh:
            await ctx.rescan_tracker(check_remote=True)
        online = {}
        for pid in ctx.nodes.peer_ids():
            try:
                peer_data = await ctx.nodes.json(pid, "GET", "/api/internal/repo-tracker", params={"refresh": str(refresh).lower()}, timeout=25.0)
                from ..repo_tracker import ingest_peer_tracker_payload

                ingest_peer_tracker_payload(ctx.tracker_conn, pid, peer_data, data_dir=ctx.data_dir, vault_dir=ctx.vault_dir())
                online[pid] = True
            except Exception:
                online[pid] = False
        rows = read_three_way_status(ctx.tracker_conn)
        return {"nodeId": ctx.nodes.local_id, "peersOnline": online, "repos": rows, "counts": hold_counts(ctx.tracker_conn)}

    @r.post("/api/repos/holds")
    async def set_hold(body: HoldSetBody, request: Request) -> dict[str, Any]:
        try:
            hold = set_repo_hold(
                ctx.tracker_conn,
                body.repoName,
                body.state,
                set_by=f"{ctx.nodes.local_id}:{zone(request)}",
                note=body.note,
                data_dir=ctx.data_dir,
                vault_dir=ctx.vault_dir(),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        for pid in ctx.nodes.peer_ids():
            try:
                await ctx.nodes.json(pid, "POST", "/api/internal/repo-holds", json=hold, timeout=5.0)
            except Exception:
                continue
        counts = hold_counts(ctx.tracker_conn)
        await ctx.emit({"type": "repo_holds_updated", "repo": hold["repoName"], "state": hold["state"], "counts": counts})
        return {"ok": True, "hold": hold, "counts": counts}

    @r.post("/api/repos/preflight")
    async def preflight(body: PreflightBody, request: Request) -> dict[str, Any]:
        node = body.node
        if not node and getattr(request.state, "auth", "") == "token":
            node = request.headers.get("x-orch-node", "")
        return await ctx.repo_sync.preflight(repo=body.repo, cwd=body.cwd, node=node, source=body.source)

    @r.post("/api/repos/handoff")
    async def handoff(body: HandoffBody, request: Request) -> dict[str, Any]:
        job = ctx.jobs.start("repo_handoff", f"Hand off {body.repo}", lambda _jid: ctx.repo_sync.handoff(body.repo, body.toNode or ctx.nodes.local_id, body.fromNode, reason=f"manual:{zone(request)}"))
        return {"jobId": job}

    @r.post("/api/sync/git-pull")
    async def git_pull(body: GitPullBody) -> dict[str, Any]:
        held, note = is_repo_held(ctx.tracker_conn, body.repoName)
        if held:
            return {"ok": False, "status": "held_active", "held": True, "message": f"{body.repoName} is locked under active development ({note})."}
        path = find_repo(ctx.github_dir(), body.repoName)
        if not path:
            raise HTTPException(404, "Repo not found")
        res = await asyncio.to_thread(pull_local_repo_ff, path)
        await ctx.rescan_repos()
        return res

    @r.post("/api/repos/sync")
    async def repo_sync(body: RepoSyncBody) -> dict[str, Any]:
        return await ctx.repo_sync.sync_action(body.repoName, body.action, body.targetNode, body.agent)

    @r.get("/api/tools/inventory")
    async def tool_inventory(localOnly: bool = False) -> dict[str, Any]:
        s = ctx.settings()
        models = await discover_node_models(s, ctx.nodes.local_id)
        tools = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        sync_local_tool_inventory(ctx.conn, ctx.nodes.local_id, models, tools, ctx.vault_dir())
        if localOnly:
            return {"nodeId": ctx.nodes.local_id, "tools": read_tool_inventory(ctx.conn, ctx.nodes.local_id)}
        for pid in ctx.nodes.peer_ids():
            try:
                upsert_peer_tools(ctx.conn, pid, (await ctx.nodes.json(pid, "GET", "/api/tools/inventory", params={"localOnly": "true"}, timeout=8.0)).get("tools", []))
            except Exception:
                continue
        return {"nodeId": ctx.nodes.local_id, "items": read_tool_inventory(ctx.conn)}

    @r.get("/api/sync/queue")
    def queue() -> dict[str, Any]:
        return {"items": list_intermission_queue(ctx.conn)}

    @r.post("/api/sync/enqueue")
    def enqueue(body: IntermissionEnqueueBody) -> dict[str, Any]:
        peer = body.targetNode or (ctx.nodes.peer_ids() or [""])[0]
        return {"ok": True, "id": ctx.enqueue_for(peer, body.category, body.target, body.payload) if peer else enqueue_intermission_event(ctx.conn, body.category, body.target, body.payload)}

    @r.post("/api/sync/catchup")
    async def catchup() -> dict[str, Any]:
        return {pid: await ctx.catchup(pid) for pid in ctx.nodes.peer_ids() if (await ctx.nodes.check(pid))[0]}

    @r.get("/api/replication/status")
    def replication_status() -> dict[str, Any]:
        rows = ctx.conn.execute("SELECT * FROM sync_state").fetchall()
        return {"last": ctx.replicator.last, "watermarks": [dict(x) for x in rows]}

    @r.post("/api/replication/run")
    async def replication_run() -> dict[str, Any]:
        return await ctx.replicator.run_once()

    # ---- vault -------------------------------------------------------------------------------
    @r.get("/api/vault/list")
    def vault_list() -> dict[str, Any]:
        v = ctx.vault_dir()
        v.mkdir(parents=True, exist_ok=True)
        return {"vaultDir": str(v), "files": list_vault_files(v)}

    @r.get("/api/vault/read")
    def vault_read(relPath: str) -> dict[str, Any]:
        try:
            target = resolve_vault_path(ctx.vault_dir(), relPath)
        except ValueError as exc:
            raise HTTPException(403, str(exc))
        if not target.is_file():
            raise HTTPException(404, "Vault file not found")
        if target.stat().st_size > 2_000_000:
            raise HTTPException(413, "Vault file too large for inline viewer")
        return {"relPath": relPath, "path": str(target), "content": target.read_text(encoding="utf-8", errors="replace")}

    def _vault_write(rel: str, content: str = "", b64: str = "") -> dict[str, Any]:
        try:
            target = resolve_vault_path(ctx.vault_dir(), rel)
        except ValueError as exc:
            raise HTTPException(403, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        if b64:
            target.write_bytes(base64.b64decode(b64, validate=True))
        else:
            target.write_text(content, encoding="utf-8")
        return {"ok": True, "relPath": rel, "path": str(target)}

    @r.post("/api/vault/write")
    def vault_write(body: VaultWriteBody) -> dict[str, Any]:
        return _vault_write(body.relPath, body.content, body.contentBase64)

    @r.post("/api/vault/copy-from-node")
    async def vault_copy(body: VaultCopyBody) -> dict[str, Any]:
        rel = body.destRelPath.strip() or Path(body.sourcePath).name
        if ctx.nodes.is_local(body.sourceNode):
            src = path_allowed(body.sourcePath, read_roots(ctx.settings(), ctx.data_dir))
            if not src or not src.is_file():
                raise HTTPException(404, "Source file not found in an allowed folder")
            content = src.read_text(encoding="utf-8", errors="replace")
        else:
            content = str((await ctx.nodes.json(body.sourceNode, "GET", "/api/file", params={"path": body.sourcePath}, timeout=30.0)).get("content") or "")
        return _vault_write(rel, content)

    return r
