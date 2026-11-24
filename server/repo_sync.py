"""Keeps a repo identical on every node, so it doesn't matter which machine you code on.

  * `repo_activity` records which node you're working on, per repo (the "active device").
  * `preflight` is called whenever work starts in a repo (agent turn, terminal, MCP tool,
    Claude SessionStart hook). If another node has newer or uncommitted work, it is handed
    off here first: committed via a wip/ ref, sent directly node-to-node as a git bundle,
    and also pushed to GitHub as a backup.
  * Anything that would need a merge stops and is reported; nothing is overwritten.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import time
from typing import Any
import uuid

from . import repo_ops
from .repo_tracker import is_repo_held
from .inventory_sync import (
    compare_repo_inventories,
    enqueue_intermission_event,
    pull_local_repo_ff,
    read_repo_inventory,
)
from .nodes import PeerOffline


class RepoSync:
    def __init__(self, ctx):
        self.ctx = ctx
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- local ops (run on this node) --------------------------------------------------------------
    def local_repo(self, name: str) -> Path:
        p = repo_ops.find_repo(self.ctx.github_dir(), name)
        if not p:
            raise repo_ops.RepoOpError(f"repo '{name}' not found on {self.ctx.nodes.local_id}")
        return p

    async def local_op(self, op: str, payload: dict[str, Any], bundle: bytes = b"") -> Any:
        nid = self.ctx.nodes.local_id
        repo = self.local_repo(str(payload["repo"]))
        if op == "snapshot":
            return await asyncio.to_thread(repo_ops.snapshot, repo, nid)
        if op == "capture":
            return await asyncio.to_thread(repo_ops.capture_wip, repo, nid)
        if op == "push":
            return await asyncio.to_thread(repo_ops.push_to_origin, repo, str(payload["branch"]), str(payload.get("wipRef") or ""))
        if op == "mark":
            return await asyncio.to_thread(repo_ops.mark_handed_off, repo, str(payload["branch"]), str(payload["sha"]))
        if op == "bundle":
            refs = [r for r in payload.get("refs") or [] if r]
            return await asyncio.to_thread(repo_ops.make_bundle, repo, refs, str(payload.get("basis") or ""))
        if op == "apply":
            return await asyncio.to_thread(
                repo_ops.apply_handoff,
                repo,
                nid,
                str(payload["fromNode"]),
                bundle,
                str(payload["branch"]),
                str(payload["head"]),
                str(payload.get("wipSha") or ""),
                str(payload.get("bundleSha256") or ""),
                str(payload.get("treeSha256") or ""),
            )
        raise repo_ops.RepoOpError(f"unknown repo op {op}")

    async def op(self, node: str, op: str, payload: dict[str, Any], bundle: bytes = b"") -> Any:
        if self.ctx.nodes.is_local(node):
            return await self.local_op(op, payload, bundle)
        if op == "bundle":
            r = await self.ctx.nodes.request(node, "POST", "/api/internal/repo/bundle", json=payload, timeout=600.0)
            if r.status_code >= 400:
                raise repo_ops.RepoOpError(f"{node}: {r.text[:300]}")
            return r.content
        if op == "apply":
            r = await self.ctx.nodes.request(node, "POST", "/api/internal/repo/apply", timeout=600.0,
                                             json={**payload, "bundleB64": base64.b64encode(bundle).decode()})
        else:
            r = await self.ctx.nodes.request(node, "POST", f"/api/internal/repo/{op}", json=payload, timeout=300.0)
        if r.status_code >= 400:
            raise repo_ops.RepoOpError(f"{node} {op}: {r.text[:300]}")
        return r.json()

    # ---- activity ----------------------------------------------------------------------------------
    def mark_active(self, repo: str, node: str, source: str, branch: str = "") -> None:
        key = repo.lower()
        now = time.time()
        row = self.ctx.conn.execute("SELECT node_id FROM repo_activity WHERE repo_key=?", (key,)).fetchone()
        if row and row["node_id"] == node:
            self.ctx.conn.execute("UPDATE repo_activity SET last_seen=?, source=?, branch=COALESCE(NULLIF(?, ''), branch) WHERE repo_key=?",
                                  (now, source, branch, key))
        else:
            self.ctx.conn.execute(
                "INSERT INTO repo_activity (repo_key, repo_name, node_id, source, branch, since, last_seen) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(repo_key) DO UPDATE SET node_id=excluded.node_id, source=excluded.source, branch=excluded.branch, since=excluded.since, last_seen=excluded.last_seen",
                (key, repo, node, source, branch, now, now))
        self.ctx.conn.commit()

    def activity(self) -> dict[str, dict[str, Any]]:
        rows = self.ctx.conn.execute("SELECT * FROM repo_activity").fetchall()
        return {r["repo_key"]: {"repo": r["repo_name"], "nodeId": r["node_id"], "source": r["source"], "branch": r["branch"],
                                "since": r["since"], "lastSeen": r["last_seen"]} for r in rows}

    def repo_from_path(self, cwd: str) -> str | None:
        try:
            rel = Path(cwd).expanduser().resolve().relative_to(self.ctx.github_dir().resolve())
        except (ValueError, OSError):
            return None
        return rel.parts[0] if rel.parts else None

    # ---- handoff ---------------------------------------------------------------------------------------
    @staticmethod
    def _has_new_work(src: dict[str, Any], dst: dict[str, Any]) -> bool:
        if src.get("appliedSha"):
            return not src["matchesApplied"]  # untouched since we last handed it away -> nothing new
        return src["dirty"] or src["headFull"] != dst["headFull"] or src["branch"] != dst["branch"]

    async def handoff(self, repo: str, to_node: str, from_node: str = "", reason: str = "manual") -> dict[str, Any]:
        held, note = is_repo_held(self.ctx.tracker_conn, repo)
        if held:
            return {"status": "held_active", "held": True, "message": f"{repo} is locked under active development ({note}). Unlock it in Repo Locks before syncing."}
        lock = self._locks.setdefault(repo.lower(), asyncio.Lock())
        async with lock:
            return await self._handoff(repo, to_node, from_node, reason)

    async def _handoff(self, repo: str, to_node: str, from_node: str, reason: str) -> dict[str, Any]:
        to_node = self.ctx.nodes.local_id if self.ctx.nodes.is_local(to_node) else to_node
        all_nodes = [self.ctx.nodes.local_id] + self.ctx.nodes.peer_ids()
        candidates = [from_node] if from_node else [n for n in all_nodes if n != to_node]
        hid = f"hand_{uuid.uuid4().hex[:10]}"
        to_snap = await self.op(to_node, "snapshot", {"repo": repo})
        for src in candidates:
            try:
                src_snap = await self.op(src, "snapshot", {"repo": repo})
            except (PeerOffline, repo_ops.RepoOpError) as exc:
                result = {"status": "source_unavailable", "message": str(exc)}
                continue
            if not self._has_new_work(src_snap, to_snap):
                result = {"status": "already_in_sync", "message": f"{repo} is identical on {src} and {to_node}."}
                continue
            cap = await self.op(src, "capture", {"repo": repo})
            pushed: dict[str, Any] = {}
            if (self.ctx.settings().get("repoSync") or {}).get("pushWipToGitHub", True):
                try:
                    pushed = await self.op(src, "push", {"repo": repo, "branch": cap["branch"], "wipRef": cap["wipRef"]})
                except Exception as exc:
                    pushed = {"error": str(exc)[:200]}
            refs = [f"refs/heads/{cap['branch']}"] + ([f"refs/heads/{cap['wipRef']}"] if cap["wipRef"] else [])
            bundle = await self.op(src, "bundle", {"repo": repo, "refs": refs, "basis": to_snap.get("headFull") or ""})
            b_hash = repo_ops.bytes_sha256(bundle)
            t_hash = str(cap.get("treeSha256") or "")
            applied = await self.op(
                to_node,
                "apply",
                {
                    "repo": repo,
                    "fromNode": src,
                    "branch": cap["branch"],
                    "head": cap["head"],
                    "wipSha": cap["wipSha"],
                    "bundleSha256": b_hash,
                    "treeSha256": t_hash,
                },
                bundle,
            )
            if applied.get("status") == "handed_off":
                await self.op(src, "mark", {"repo": repo, "branch": cap["branch"], "sha": cap["markSha"]})
            result = {**applied, "from": src, "to": to_node, "wipRef": cap["wipRef"], "github": pushed}
            self.ctx.conn.execute(
                "INSERT INTO repo_handoffs (id, repo_name, from_node, to_node, status, wip_ref, wip_sha, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (hid, repo, src, to_node, applied.get("status", "?"), cap["wipRef"], cap["wipSha"], json.dumps({**result, "reason": reason}), time.time()))
            self.ctx.conn.commit()
            self.ctx.audit.record("repo_handoff", repo, frm=src, to=to_node, status=applied.get("status"), reason=reason)
            await self.ctx.emit({"type": "repo_handoff", "repo": repo, **{k: v for k, v in result.items() if k != "github"}})
            if applied.get("status") != "handed_off":
                await self.ctx.alerts.send(f"{repo}: handoff needs you", str(applied.get("message") or applied.get("status")), key=f"handoff:{repo}")
            return result
        return locals().get("result") or {"status": "no_source", "message": "No other node to hand off from."}

    async def preflight(self, *, repo: str = "", cwd: str = "", node: str = "", source: str = "agent") -> dict[str, Any]:
        """Called before work starts. Returns a short human message an agent can read."""
        node = node if node and not self.ctx.nodes.is_local(node) else self.ctx.nodes.local_id
        name = repo or (self.repo_from_path(cwd) if cwd else None)
        if not name:
            return {"status": "not_a_repo", "message": ""}
        held, note = is_repo_held(self.ctx.tracker_conn, name)
        if held:
            return {"status": "held_active", "held": True, "message": f"{name} is locked under active development ({note}); preflight left files untouched."}
        prev = self.activity().get(name.lower())
        self.mark_active(name, node, source)
        auto = (self.ctx.settings().get("repoSync") or {}).get("autoHandoff", True)
        if not auto:
            return {"status": "marked", "message": f"{name}: marked active on {node}."}
        others = [n for n in [self.ctx.nodes.local_id] + self.ctx.nodes.peer_ids() if n != node]
        reachable = []
        for o in others:
            if self.ctx.nodes.is_local(o) or (await self.ctx.nodes.check(o))[0]:
                reachable.append(o)
        if not reachable:
            return {"status": "peers_offline", "message": (
                f"{name}: the other machine is offline, so it can't be checked for newer work. "
                f"Last active device: {prev['nodeId'] if prev else 'unknown'}.")}
        try:
            res = await self.handoff(name, node, from_node=(prev["nodeId"] if prev and prev["nodeId"] in reachable else ""), reason=f"preflight:{source}")
        except Exception as exc:
            return {"status": "error", "message": f"{name}: sync check failed: {exc}"}
        msgs = {
            "already_in_sync": f"{name} is in sync across machines.",
            "handed_off": f"{name}: brought over work from {res.get('from')} ({'; '.join(res.get('actions') or []) or 'no changes needed'}).",
        }
        return {**res, "message": msgs.get(res.get("status", ""), f"⚠️ {name}: {res.get('message') or res.get('status')}")}

    # ---- classic pull / reconcile -------------------------------------------------------------------
    async def sync_action(self, repo: str, action: str, target: str, agent: str) -> dict[str, Any]:
        name = Path(repo).name
        held, note = is_repo_held(self.ctx.tracker_conn, name)
        if held:
            return {"ok": False, "status": "held_active", "held": True, "message": f"{name} is locked under active development ({note}). Unlock it in Repo Locks before syncing."}
        if action == "handoff":
            return await self.handoff(name, target if target not in ("", "both") else self.ctx.nodes.local_id)
        if action == "reconcile":
            cmp_rows = compare_repo_inventories(read_repo_inventory(self.ctx.conn))
            row = next((x for x in cmp_rows if x["repoName"].lower() == name.lower()), None)
            prompt = (f"Reconcile repository `{name}` so every machine and GitHub origin are in sync.\n"
                      f"Inventory comparison:\n```json\n{json.dumps(row, indent=2, default=str)}\n```\n"
                      "Inspect branches (`git branch -a -vv`) and `git status`. Do NOT lose work; report exact commands and final HEAD.")
            node = target if target not in ("", "both") else self.ctx.nodes.local_id
            path = repo_ops.find_repo(self.ctx.github_dir(), name)
            return {"agentResult": await self.ctx.agents.dispatch(node, agent, prompt, cwd=str(path or self.ctx.github_dir()), on_offline="fail")}
        results: dict[str, Any] = {"repoName": name, "action": "pull"}
        nodes = [self.ctx.nodes.local_id] + self.ctx.nodes.peer_ids() if target in ("", "both") else [target]
        for n in nodes:
            if self.ctx.nodes.is_local(n):
                path = repo_ops.find_repo(self.ctx.github_dir(), name)
                results[n] = await asyncio.to_thread(pull_local_repo_ff, path) if path else {"ok": False, "status": "missing"}
            else:
                try:
                    results[n] = await self.ctx.nodes.json(n, "POST", "/api/sync/git-pull", json={"repoName": name}, timeout=120.0)
                except PeerOffline:
                    eid = enqueue_intermission_event(self.ctx.conn, "repo_sync", name, {"requestedTarget": n})
                    self.ctx.conn.execute("UPDATE desktop_intermission_queue SET target_node=? WHERE id=?", (n, eid))
                    self.ctx.conn.commit()
                    results[n] = {"ok": False, "status": "queued_for_catchup", "queueId": eid}
        return results

    def recent_handoffs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.ctx.conn.execute("SELECT * FROM repo_handoffs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "repo": r["repo_name"], "from": r["from_node"], "to": r["to_node"], "status": r["status"],
                 "wipRef": r["wip_ref"], "detail": json.loads(r["detail_json"] or "{}"), "createdAt": r["created_at"]} for r in rows]
