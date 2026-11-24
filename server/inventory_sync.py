"""Per-node GitHub repo inventory DB, tool inventory DB, ~/LLM-Vault/ storage, and Desktop intermission catch-up."""

from __future__ import annotations

import asyncio
import hashlib
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Awaitable, Callable

import httpx


def _git_output(repo_dir: Path, *args: str, timeout: float = 4.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:
        return 1, ""


def scan_single_repo(repo_dir: Path, node_id: str) -> dict[str, Any]:
    """Inspect a single git repo's branch, HEAD SHA, upstream ahead/behind, branches, and dirty state."""
    _, branch = _git_output(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    _, head_sha = _git_output(repo_dir, "rev-parse", "--short", "HEAD")
    rc_up, upstream = _git_output(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead, behind = 0, 0
    if rc_up == 0 and upstream:
        rc_lr, counts = _git_output(repo_dir, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if rc_lr == 0 and counts:
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    else:
        upstream = ""

    _, status_out = _git_output(repo_dir, "status", "--porcelain")
    dirty_lines: list[str] = []
    dirty_count = 0
    untracked_count = 0
    for line in status_out.splitlines():
        if not line:
            continue
        dirty_lines.append(line[:140])
        if line.startswith("??"):
            untracked_count += 1
        else:
            dirty_count += 1

    _, br_out = _git_output(repo_dir, "branch", "--format=%(refname:short)")
    branches = [b.strip() for b in br_out.splitlines() if b.strip()]

    _, last_commit_at = _git_output(repo_dir, "log", "-1", "--format=%cI")
    return {
        "nodeId": node_id,
        "repoName": repo_dir.name,
        "path": str(repo_dir),
        "branch": branch or "HEAD",
        "headSha": head_sha,
        "upstreamRemote": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirtyCount": dirty_count,
        "untrackedCount": untracked_count,
        "branches": branches,
        "dirtyFiles": dirty_lines[:40],
        "lastCommitAt": last_commit_at,
        "scannedAt": time.time(),
    }


def scan_local_repos(conn: sqlite3.Connection, github_dir: Path, node_id: str) -> list[dict[str, Any]]:
    """Scan all git repositories in the repos folder (paths.githubDir) and upsert into `repo_inventory`."""
    results: list[dict[str, Any]] = []
    if github_dir.is_dir():
        dirs = sorted(
            [p for p in github_dir.iterdir() if p.is_dir() and (p / ".git").exists()],
            key=lambda p: p.name.lower(),
        )
        for repo_dir in dirs:
            results.append(scan_single_repo(repo_dir, node_id))

    now = time.time()
    seen_names = {r["repoName"] for r in results}
    conn.execute("DELETE FROM repo_inventory WHERE node_id=?", (node_id,))
    for r in results:
        conn.execute(
            """INSERT OR REPLACE INTO repo_inventory
            (node_id, repo_name, path, branch, head_sha, upstream_remote, ahead, behind,
             dirty_count, untracked_count, branches_json, dirty_json, last_commit_at, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                node_id,
                r["repoName"],
                r["path"],
                r["branch"],
                r["headSha"],
                r["upstreamRemote"],
                r["ahead"],
                r["behind"],
                r["dirtyCount"],
                r["untrackedCount"],
                json.dumps(r["branches"]),
                json.dumps(r["dirtyFiles"]),
                r["lastCommitAt"],
                now,
            ),
        )
    conn.commit()
    return results


def upsert_peer_repos(conn: sqlite3.Connection, peer_node_id: str, repos: list[dict[str, Any]]) -> None:
    now = time.time()
    conn.execute("DELETE FROM repo_inventory WHERE node_id=?", (peer_node_id,))
    for r in repos:
        conn.execute(
            """INSERT OR REPLACE INTO repo_inventory
            (node_id, repo_name, path, branch, head_sha, upstream_remote, ahead, behind,
             dirty_count, untracked_count, branches_json, dirty_json, last_commit_at, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                peer_node_id,
                str(r.get("repoName") or ""),
                str(r.get("path") or ""),
                str(r.get("branch") or ""),
                str(r.get("headSha") or ""),
                str(r.get("upstreamRemote") or ""),
                int(r.get("ahead") or 0),
                int(r.get("behind") or 0),
                int(r.get("dirtyCount") or 0),
                int(r.get("untrackedCount") or 0),
                json.dumps(r.get("branches") or []),
                json.dumps(r.get("dirtyFiles") or []),
                str(r.get("lastCommitAt") or ""),
                float(r.get("scannedAt") or now),
            ),
        )
    conn.commit()


def read_repo_inventory(conn: sqlite3.Connection, node_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM repo_inventory"
    params: tuple[Any, ...] = ()
    if node_id:
        sql += " WHERE node_id=?"
        params = (node_id,)
    sql += " ORDER BY repo_name COLLATE NOCASE ASC, node_id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "nodeId": r["node_id"],
            "repoName": r["repo_name"],
            "path": r["path"],
            "branch": r["branch"],
            "headSha": r["head_sha"],
            "upstreamRemote": r["upstream_remote"],
            "ahead": r["ahead"],
            "behind": r["behind"],
            "dirtyCount": r["dirty_count"],
            "untrackedCount": r["untracked_count"],
            "branches": json.loads(r["branches_json"] or "[]"),
            "dirtyFiles": json.loads(r["dirty_json"] or "[]"),
            "lastCommitAt": r["last_commit_at"],
            "scannedAt": r["scanned_at"],
        }
        for r in rows
    ]


def compare_repo_inventories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build per-repo comparison between Laptop and Desktop plus sync status."""
    by_repo: dict[str, dict[str, dict[str, Any]]] = {}
    canonical_name: dict[str, str] = {}
    for r in rows:
        key = r["repoName"].lower()
        by_repo.setdefault(key, {})[r["nodeId"]] = r
        # Prefer capitalized mixed-case name when one exists (e.g. VibeBuilder-IDE vs vibebuilder-ide)
        prev = canonical_name.get(key)
        if not prev or (prev.islower() and not r["repoName"].islower()):
            canonical_name[key] = r["repoName"]

    summary: list[dict[str, Any]] = []
    for key in sorted(by_repo.keys()):
        repo_name = canonical_name[key]
        nodes = by_repo[key]
        lap = nodes.get("laptop")
        desk = nodes.get("desktop")
        if lap and desk:
            if lap["dirtyCount"] > 0 or desk["dirtyCount"] > 0:
                status = "dirty"
            elif lap["branch"] != desk["branch"]:
                status = "branch_mismatch"
            elif lap["headSha"] and desk["headSha"] and lap["headSha"] == desk["headSha"]:
                status = "behind_remote" if (lap["behind"] > 0 or desk["behind"] > 0) else "in_sync"
            else:
                status = "diverged"
        elif lap and not desk:
            status = "laptop_only"
        else:
            status = "desktop_only"
        summary.append(
            {
                "repoName": repo_name,
                "status": status,
                "laptop": lap,
                "desktop": desk,
            }
        )
    return summary


# ---------------------------------------------------------------------------
# Per-Node Tool Inventory DB
# ---------------------------------------------------------------------------

def sync_local_tool_inventory(
    conn: sqlite3.Connection,
    node_id: str,
    models: list[dict[str, Any]],
    python_tools: dict[str, Any],
    vault_dir: Path | None = None,
) -> list[dict[str, Any]]:
    now = time.time()
    conn.execute("DELETE FROM tool_inventory WHERE node_id=?", (node_id,))
    entries: list[dict[str, Any]] = []

    for m in models:
        kind = "cli_agent" if m["id"] in ("agy-cli", "claude-cli") else m["kind"]
        tid = f"{kind}:{m['id']}"
        meta = {
            "strengths": m.get("strengths", []),
            "fit": m.get("fit", ""),
            "contextLength": m.get("contextLength", 0),
        }
        entries.append(
            {
                "nodeId": node_id,
                "toolId": tid,
                "kind": kind,
                "name": m["displayName"],
                "status": m["status"],
                "metadata": meta,
                "updatedAt": now,
            }
        )

    for tname, tinfo in python_tools.items():
        entries.append(
            {
                "nodeId": node_id,
                "toolId": f"python:{tname}",
                "kind": "python_plugin",
                "name": tname,
                "status": "ready" if tinfo.get("enabled") else "disabled",
                "metadata": {
                    "category": tinfo.get("category"),
                    "description": tinfo.get("description"),
                    "gpuNeed": tinfo.get("gpuNeed"),
                    "path": tinfo.get("path"),
                },
                "updatedAt": now,
            }
        )

    if vault_dir is not None:
        entries.append(
            {
                "nodeId": node_id,
                "toolId": "storage:vault",
                "kind": "vault",
                "name": f"Hub Vault ({vault_dir})",
                "status": "ready" if vault_dir.exists() else "uninitialized",
                "metadata": {"path": str(vault_dir)},
                "updatedAt": now,
            }
        )

    for e in entries:
        conn.execute(
            """INSERT OR REPLACE INTO tool_inventory
            (node_id, tool_id, kind, name, status, metadata_json, updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                e["nodeId"],
                e["toolId"],
                e["kind"],
                e["name"],
                e["status"],
                json.dumps(e["metadata"]),
                now,
            ),
        )
    conn.commit()
    return entries


def upsert_peer_tools(conn: sqlite3.Connection, peer_node_id: str, tools: list[dict[str, Any]]) -> None:
    now = time.time()
    conn.execute("DELETE FROM tool_inventory WHERE node_id=?", (peer_node_id,))
    for t in tools:
        conn.execute(
            """INSERT OR REPLACE INTO tool_inventory
            (node_id, tool_id, kind, name, status, metadata_json, updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                peer_node_id,
                str(t.get("toolId") or ""),
                str(t.get("kind") or "tool"),
                str(t.get("name") or ""),
                str(t.get("status") or "ready"),
                json.dumps(t.get("metadata") or {}),
                float(t.get("updatedAt") or now),
            ),
        )
    conn.commit()


def read_tool_inventory(conn: sqlite3.Connection, node_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tool_inventory"
    params: tuple[Any, ...] = ()
    if node_id:
        sql += " WHERE node_id=?"
        params = (node_id,)
    sql += " ORDER BY node_id ASC, kind ASC, name COLLATE NOCASE ASC"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "nodeId": r["node_id"],
            "toolId": r["tool_id"],
            "kind": r["kind"],
            "name": r["name"],
            "status": r["status"],
            "metadata": json.loads(r["metadata_json"] or "{}"),
            "updatedAt": r["updated_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Laptop Hub Shared Vault (~/LLM-Vault/)
# ---------------------------------------------------------------------------

def resolve_vault_path(vault_root: Path, rel_path: str) -> Path:
    clean = rel_path.strip().lstrip("/\\")
    if not clean:
        raise ValueError("Empty vault path")
    root_res = vault_root.resolve()
    target = (root_res / clean).resolve()
    if root_res not in target.parents and target != root_res:
        raise ValueError("Path escapes Vault root")
    return target


def list_vault_files(vault_root: Path) -> list[dict[str, Any]]:
    if not vault_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rp = Path(root)
        for fn in files:
            if fn.startswith("."):
                continue
            p = rp / fn
            try:
                st = p.stat()
                mt, _ = mimetypes.guess_type(fn)
                out.append(
                    {
                        "relPath": str(p.relative_to(vault_root)).replace("\\", "/"),
                        "absPath": str(p),
                        "size": st.st_size,
                        "modifiedMs": int(st.st_mtime * 1000),
                        "mimeType": mt or "application/octet-stream",
                    }
                )
            except OSError:
                continue
    out.sort(key=lambda x: x["modifiedMs"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Intermission Queue & Automatic Catch-Up upon Desktop Reconnect
# ---------------------------------------------------------------------------

def enqueue_intermission_event(
    conn: sqlite3.Connection,
    category: str,
    target: str,
    payload: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO desktop_intermission_queue
        (created_at, category, target, payload_json, status, result_note)
        VALUES (?,?,?,?,?,?)""",
        (time.time(), category, target, json.dumps(payload or {}), "queued", ""),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_intermission_queue(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM desktop_intermission_queue ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "createdAt": r["created_at"],
            "category": r["category"],
            "target": r["target"],
            "payload": json.loads(r["payload_json"] or "{}"),
            "status": r["status"],
            "resultNote": r["result_note"],
            "syncedAt": r["synced_at"],
        }
        for r in rows
    ]


def pull_local_repo_ff(repo_dir: Path) -> dict[str, Any]:
    """Safely fetch & fast-forward pull a single clean repo; never overwrites dirty state or >1 diverged branch."""
    if not (repo_dir / ".git").exists():
        return {"ok": False, "status": "missing", "message": f"Not found: {repo_dir}"}
    info = scan_single_repo(repo_dir, "local")
    if info["dirtyCount"] > 0 or info["untrackedCount"] > 0:
        return {
            "ok": False,
            "status": "conflict_dirty",
            "message": f"{repo_dir.name} has {info['dirtyCount']} modified / {info['untrackedCount']} untracked files.",
            "repo": info,
        }
    _git_output(repo_dir, "fetch", "--all", "--prune", timeout=20.0)
    refreshed = scan_single_repo(repo_dir, "local")
    if len(refreshed["branches"]) > 1:
        # Check if any secondary branch has unmerged commits not in current HEAD
        for br in refreshed["branches"]:
            if br == refreshed["branch"]:
                continue
            rc, unmerged = _git_output(repo_dir, "log", "--oneline", f"HEAD..{br}")
            if rc == 0 and unmerged.strip():
                return {
                    "ok": False,
                    "status": "conflict_multi_branch",
                    "message": f"{repo_dir.name} has unmerged commits on branch '{br}'; ask user or agent before syncing.",
                    "repo": refreshed,
                }
    if not refreshed["upstreamRemote"]:
        return {"ok": True, "status": "no_upstream", "message": f"{repo_dir.name} has no upstream tracking branch.", "repo": refreshed}
    _, head_before = _git_output(repo_dir, "rev-parse", "HEAD")
    rc, out = _git_output(repo_dir, "pull", "--ff-only", timeout=25.0)
    final_info = scan_single_repo(repo_dir, "local")
    _, head_after = _git_output(repo_dir, "rev-parse", "HEAD")
    sha_before = hashlib.sha256(head_before.encode()).hexdigest() if head_before else ""
    sha_after = hashlib.sha256(head_after.encode()).hexdigest() if head_after else ""
    if rc == 0:
        return {
            "ok": True,
            "status": "pulled",
            "message": out or "Up to date.",
            "repo": final_info,
            "sha256Before": sha_before,
            "sha256After": sha_after,
            "sha256Verified": bool(sha_after),
        }
    return {"ok": False, "status": "conflict_diverged", "message": out or "Fast-forward pull failed.", "repo": final_info}


async def run_desktop_catchup(
    conn: sqlite3.Connection,
    desktop_url: str,
    peer_headers: dict[str, str],
    broadcast: Callable[[dict[str, Any]], Awaitable[None]],
    github_dir: Path,
) -> dict[str, Any]:
    """Process all queued intermission events cleanly when Desktop transitions offline -> online."""
    queued = conn.execute(
        "SELECT * FROM desktop_intermission_queue WHERE status='queued' ORDER BY id ASC"
    ).fetchall()
    if not queued:
        return {"processed": 0, "synced": 0, "conflicts": 0}

    total = len(queued)
    synced = 0
    conflicts = 0
    await broadcast({"type": "catchup_status", "running": True, "total": total, "done": 0, "message": f"Catching up {total} intermission changes on Desktop…"})

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Refresh Desktop inventory first
        try:
            inv_r = await client.get(f"{desktop_url.rstrip('/')}/api/repos/inventory?localOnly=1&refresh=true", headers=peer_headers)
            if inv_r.status_code == 200:
                upsert_peer_repos(conn, "desktop", inv_r.json().get("localRepos", []))
        except Exception:
            pass

        for idx, row in enumerate(queued):
            eid = row["id"]
            cat = row["category"]
            target = row["target"]
            conn.execute("UPDATE desktop_intermission_queue SET status='reconciling' WHERE id=?", (eid,))
            conn.commit()

            if cat in ("repo_commit", "repo_sync"):
                try:
                    pr = await client.post(
                        f"{desktop_url.rstrip('/')}/api/sync/git-pull",
                        json={"repoName": target},
                        headers=peer_headers,
                    )
                    res = pr.json() if pr.status_code == 200 else {"ok": False, "message": pr.text[:200]}
                    if res.get("ok"):
                        conn.execute(
                            "UPDATE desktop_intermission_queue SET status='synced', result_note=?, synced_at=? WHERE id=?",
                            (str(res.get("message") or "Fast-forwarded on Desktop"), time.time(), eid),
                        )
                        synced += 1
                    else:
                        conn.execute(
                            "UPDATE desktop_intermission_queue SET status='conflict_needs_agent', result_note=?, synced_at=? WHERE id=?",
                            (str(res.get("message") or "Needs manual/agent reconciliation"), time.time(), eid),
                        )
                        conflicts += 1
                except Exception as exc:
                    conn.execute(
                        "UPDATE desktop_intermission_queue SET status='conflict_needs_agent', result_note=? WHERE id=?",
                        (f"Sync request failed: {exc}", eid),
                    )
                    conflicts += 1
            elif cat in ("repo_dirty_edit",):
                conn.execute(
                    "UPDATE desktop_intermission_queue SET status='conflict_needs_agent', result_note=? WHERE id=?",
                    ("Uncommitted Laptop edits occurred while Desktop was offline — reconcile via AGY/Claude or commit & pull.", eid),
                )
                conflicts += 1
            else:
                # vault_change, tool_or_config and plugin events live in the hub vault / synced index
                note = "Recorded on Hub Vault; available immediately to Desktop Worker."
                if cat == "tool_or_config":
                    try:
                        # Mirror updated settings/tools notice
                        note = "Hub tool/config update recorded; Desktop notified."
                    except Exception:
                        pass
                conn.execute(
                    "UPDATE desktop_intermission_queue SET status='synced', result_note=?, synced_at=? WHERE id=?",
                    (note, time.time(), eid),
                )
                synced += 1

            conn.commit()
            await broadcast(
                {
                    "type": "catchup_status",
                    "running": idx + 1 < total,
                    "total": total,
                    "done": idx + 1,
                    "message": f"Catch-up {idx + 1}/{total}: {target} ({'OK' if conflicts == 0 else f'{conflicts} conflict(s)'})",
                }
            )

        # Final inventory refresh on Desktop + Laptop
        try:
            scan_local_repos(conn, github_dir, "laptop")
            inv_r2 = await client.get(f"{desktop_url.rstrip('/')}/api/repos/inventory?localOnly=1&refresh=true", headers=peer_headers)
            if inv_r2.status_code == 200:
                upsert_peer_repos(conn, "desktop", inv_r2.json().get("localRepos", []))
        except Exception:
            pass

    await broadcast({"type": "catchup_status", "running": False, "total": total, "done": total, "conflicts": conflicts, "message": ""})
    return {"processed": total, "synced": synced, "conflicts": conflicts}
