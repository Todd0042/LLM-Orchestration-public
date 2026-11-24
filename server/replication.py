"""Two-way replication between nodes: conversation history, repo activity, and the vault.

Every node pulls from every online peer ("give me rows with seq > my watermark"), so the
same conversation exists on both machines and either can keep working while the other is
away. Merges can't conflict for messages (immutable, unique ids); chats and repo activity
are last-writer-wins on their own timestamps.

Vault: three-way compare (local, remote, last-synced hash). A file changed on only one side
is copied; a file changed on both keeps the newer one at the path and the other as
`name.conflict-<node>-<hash8>.ext`, named identically on both machines so they converge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .inventory_sync import resolve_vault_path
from .nodes import PeerOffline

STREAMS: dict[str, dict[str, Any]] = {
    "chats": {"cols": ["id", "title", "workspace", "mode", "task_type", "node_id", "peer_config_json", "created_at", "updated_at", "pinned", "archived"],
              "upsert": "ON CONFLICT(id) DO UPDATE SET title=excluded.title, workspace=excluded.workspace, mode=excluded.mode, "
                        "task_type=excluded.task_type, peer_config_json=excluded.peer_config_json, updated_at=excluded.updated_at, "
                        "pinned=excluded.pinned, archived=excluded.archived "
                        "WHERE excluded.updated_at > chats.updated_at"},
    "messages": {"cols": ["id", "chat_id", "role", "sender_label", "node_id", "model_id", "content", "events_json", "created_at"],
                 "upsert": "ON CONFLICT(id) DO NOTHING"},
    "artifacts": {"cols": ["id", "chat_id", "kind", "title", "path", "node_id", "mime_type", "meta_json", "created_at"],
                  "upsert": "ON CONFLICT(id) DO NOTHING"},
    "repo_activity": {"cols": ["repo_key", "repo_name", "node_id", "source", "branch", "since", "last_seen"],
                      "upsert": "ON CONFLICT(repo_key) DO UPDATE SET repo_name=excluded.repo_name, node_id=excluded.node_id, "
                                "source=excluded.source, branch=excluded.branch, since=excluded.since, last_seen=excluded.last_seen "
                                "WHERE excluded.last_seen > repo_activity.last_seen"},
    # Public keys and hashed session ids only. Every field only moves forward (counters, times,
    # revoked 0 -> 1), so a revoke always wins and an unchanged row is never re-written (no ping-pong).
    "passkeys": {"cols": ["id", "public_key", "sign_count", "name", "created_at", "last_used_at", "revoked", "updated_at"],
                 "upsert": "ON CONFLICT(id) DO UPDATE SET sign_count=MAX(passkeys.sign_count, excluded.sign_count), "
                           "last_used_at=MAX(passkeys.last_used_at, excluded.last_used_at), revoked=MAX(passkeys.revoked, excluded.revoked), "
                           "name=CASE WHEN excluded.updated_at > passkeys.updated_at THEN excluded.name ELSE passkeys.name END, "
                           "updated_at=MAX(passkeys.updated_at, excluded.updated_at) "
                           "WHERE excluded.sign_count > passkeys.sign_count OR excluded.last_used_at > passkeys.last_used_at "
                           "OR excluded.revoked > passkeys.revoked OR (excluded.updated_at > passkeys.updated_at AND excluded.name != passkeys.name)"},
    "device_sessions": {"cols": ["id", "name", "method", "created_at", "last_seen", "expires_at", "revoked", "updated_at"],
                        "upsert": "ON CONFLICT(id) DO UPDATE SET last_seen=MAX(device_sessions.last_seen, excluded.last_seen), "
                                  "expires_at=MAX(device_sessions.expires_at, excluded.expires_at), revoked=MAX(device_sessions.revoked, excluded.revoked), "
                                  "updated_at=MAX(device_sessions.updated_at, excluded.updated_at) "
                                  "WHERE excluded.last_seen > device_sessions.last_seen OR excluded.expires_at > device_sessions.expires_at "
                                  "OR excluded.revoked > device_sessions.revoked"},
}
MAX_VAULT_FILE = 200 * 1024 * 1024


def export_rows(conn: sqlite3.Connection, stream: str, after: int, limit: int = 500) -> dict[str, Any]:
    spec = STREAMS[stream]
    cols = ", ".join(spec["cols"] + ["seq"])
    rows = conn.execute(f"SELECT {cols} FROM {stream} WHERE seq > ? ORDER BY seq ASC LIMIT ?", (after, limit)).fetchall()
    return {"stream": stream, "rows": [dict(r) for r in rows], "more": len(rows) == limit}


def import_rows(conn: sqlite3.Connection, stream: str, rows: list[dict[str, Any]]) -> int:
    spec = STREAMS[stream]
    cols = spec["cols"]
    sql = f"INSERT INTO {stream} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) {spec['upsert']}"
    before = conn.total_changes
    defaults = {"pinned": 0, "archived": 0}  # rows from a peer that hasn't migrated yet
    for r in rows:
        conn.execute(sql, tuple(r.get(c, defaults.get(c)) for c in cols))
    conn.commit()
    return conn.total_changes - before


# ---- vault -------------------------------------------------------------------------------------

class VaultIndex:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, int, str]] = {}

    def manifest(self, root: Path) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not root.is_dir():
            return out
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith(".") or fn.endswith(".orch-tmp"):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size > MAX_VAULT_FILE:
                    continue
                rel = p.relative_to(root).as_posix()
                key = str(p)
                cached = self._cache.get(key)
                if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                    digest = cached[2]
                else:
                    h = hashlib.sha256()
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    digest = h.hexdigest()
                    self._cache[key] = (st.st_size, st.st_mtime_ns, digest)
                out[rel] = {"sha256": digest, "size": st.st_size, "mtime": st.st_mtime}
        return out


def conflict_name(rel: str, node: str, sha: str) -> str:
    p = Path(rel)
    return str(p.with_name(f"{p.stem}.conflict-{node}-{sha[:8]}{p.suffix}"))


class Replicator:
    def __init__(self, ctx):
        self.ctx = ctx
        self.vault_index = VaultIndex()
        self._running = False
        self.last: dict[str, dict[str, Any]] = {}

    def _watermark(self, peer: str, stream: str) -> int:
        r = self.ctx.conn.execute("SELECT last_seq FROM sync_state WHERE peer_id=? AND stream=?", (peer, stream)).fetchone()
        return int(r["last_seq"]) if r else 0

    def _set_watermark(self, peer: str, stream: str, seq: int) -> None:
        self.ctx.conn.execute(
            "INSERT INTO sync_state (peer_id, stream, last_seq, synced_at) VALUES (?,?,?,?) "
            "ON CONFLICT(peer_id, stream) DO UPDATE SET last_seq=excluded.last_seq, synced_at=excluded.synced_at",
            (peer, stream, seq, time.time()))
        self.ctx.conn.commit()

    async def pull_db(self, peer: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for stream in STREAMS:
            after = self._watermark(peer, stream)
            total = 0
            while True:
                data = await self.ctx.nodes.json(peer, "GET", "/api/internal/replicate/pull", params={"stream": stream, "after": after}, timeout=60.0)
                rows = data.get("rows") or []
                if rows:
                    total += import_rows(self.ctx.conn, stream, rows)
                    after = max(int(r["seq"]) for r in rows)
                    self._set_watermark(peer, stream, after)
                if not data.get("more"):
                    break
            counts[stream] = total
        return counts

    # ---- vault three-way sync (this node only changes its own files) -------------------------------
    def _base(self, peer: str) -> dict[str, str]:
        rows = self.ctx.conn.execute("SELECT rel_path, sha256 FROM vault_sync_state WHERE peer_id=?", (peer,)).fetchall()
        return {r["rel_path"]: r["sha256"] for r in rows}

    def _set_base(self, peer: str, rel: str, sha: str | None) -> None:
        if sha is None:
            self.ctx.conn.execute("DELETE FROM vault_sync_state WHERE peer_id=? AND rel_path=?", (peer, rel))
        else:
            self.ctx.conn.execute("INSERT OR REPLACE INTO vault_sync_state (peer_id, rel_path, sha256) VALUES (?,?,?)", (peer, rel, sha))

    async def _download(self, peer: str, rel: str, dest_rel: str, sha: str) -> bool:
        r = await self.ctx.nodes.request(peer, "GET", "/api/internal/vault/raw", params={"relPath": rel}, timeout=300.0)
        if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != sha:
            return False  # changed mid-flight; next round will retry
        dest = resolve_vault_path(self.ctx.vault_dir(), dest_rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".orch-tmp")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return True

    async def sync_vault(self, peer: str) -> dict[str, int]:
        root = self.ctx.vault_dir()
        root.mkdir(parents=True, exist_ok=True)
        local = await asyncio.to_thread(self.vault_index.manifest, root)
        remote = (await self.ctx.nodes.json(peer, "GET", "/api/internal/vault/manifest", timeout=120.0)).get("files") or {}
        base = self._base(peer)
        stats = {"downloaded": 0, "deleted": 0, "conflicts": 0}
        me = self.ctx.nodes.local_id
        for rel in sorted(set(local) | set(remote) | set(base)):
            L = (local.get(rel) or {}).get("sha256")
            R = (remote.get(rel) or {}).get("sha256")
            B = base.get(rel)
            if L == R:
                self._set_base(peer, rel, L)
                continue
            if L == B:  # only the peer changed it
                if R is None:
                    with_path = resolve_vault_path(root, rel)
                    with_path.unlink(missing_ok=True)
                    stats["deleted"] += 1
                    self._set_base(peer, rel, None)
                elif await self._download(peer, rel, rel, R):
                    stats["downloaded"] += 1
                    self._set_base(peer, rel, R)
                continue
            if R == B:  # only we changed it; the peer pulls it from us
                continue
            # changed on both sides (or created on both with different content)
            if L is None:  # we deleted, they edited: keep the edit
                if await self._download(peer, rel, rel, R):
                    stats["downloaded"] += 1
                    self._set_base(peer, rel, R)
                continue
            if R is None:  # they deleted, we edited: keep ours; they'll pull it back
                continue
            stats["conflicts"] += 1
            l_m, r_m = local[rel]["mtime"], remote[rel]["mtime"]
            remote_wins = (r_m, peer) > (l_m, me)
            if remote_wins:
                src = resolve_vault_path(root, rel)
                keep = resolve_vault_path(root, conflict_name(rel, me, L))
                keep.parent.mkdir(parents=True, exist_ok=True)
                src.replace(keep)
                if await self._download(peer, rel, rel, R):
                    self._set_base(peer, rel, R)
            else:
                # keep ours and save theirs as a copy; the base stays unchanged until the peer has
                # taken our version too (only then do both sides agree), or ours would be overwritten
                await self._download(peer, rel, conflict_name(rel, peer, R), R)
        self.ctx.conn.commit()
        return stats

    async def run_once(self) -> dict[str, Any]:
        if self._running:
            return {"skipped": "already running"}
        self._running = True
        out: dict[str, Any] = {}
        try:
            for peer in self.ctx.nodes.peer_ids():
                ok, _ = await self.ctx.nodes.check(peer)
                if not ok:
                    out[peer] = {"online": False}
                    continue
                try:
                    db = await self.pull_db(peer)
                    vault = await self.sync_vault(peer)
                    queued = await self.ctx.catchup(peer)
                    out[peer] = {"online": True, "db": db, "vault": vault, "catchup": queued, "at": time.time()}
                    if any(db.values()) or any(vault.values()):
                        await self.ctx.emit({"type": "replicated", "peer": peer, "db": db, "vault": vault})
                except (PeerOffline, RuntimeError, OSError, ValueError) as exc:
                    out[peer] = {"online": True, "error": str(exc)[:300], "at": time.time()}
            self.last = out
            return out
        finally:
            self._running = False


def vault_manifest_payload(index: VaultIndex, root: Path) -> dict[str, Any]:
    return {"files": index.manifest(root)}


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)
