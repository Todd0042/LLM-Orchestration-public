"""Small shared services: event broadcaster, audit log, command approvals, background jobs."""

from __future__ import annotations

import asyncio
import json
import shlex
import sqlite3
import time
from typing import Any, Awaitable, Callable
import uuid

from fastapi import WebSocket


class Broadcaster:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def __call__(self, event: dict[str, Any]) -> None:
        raw = json.dumps(event)
        for ws in list(self.clients):
            try:
                await ws.send_text(raw)
            except Exception:
                self.clients.discard(ws)


class AuditLog:
    """Append-only record of who (zone/client) asked for what, and where it ran."""

    def __init__(self, conn: sqlite3.Connection, node_id: Callable[[], str]):
        self.conn = conn
        self.node_id = node_id

    def record(self, action: str, target: str = "", *, zone: str = "internal", client: str = "", **detail: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, node_id, zone, client, action, target, detail_json) VALUES (?,?,?,?,?,?,?)",
            (time.time(), self.node_id(), zone, client[:64], action, target[:500], json.dumps(detail, default=str)[:8000]),
        )
        self.conn.commit()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"id": r["id"], "ts": r["ts"], "nodeId": r["node_id"], "zone": r["zone"], "client": r["client"],
             "action": r["action"], "target": r["target"], "detail": json.loads(r["detail_json"] or "{}")}
            for r in rows
        ]


# ---- Approvals for commands the orchestrator model wants to run ----------------------------------

_READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "rev-list", "ls-files", "remote", "fetch", "describe", "blame", "shortlog", "worktree", "branch", "tag", "config"}
_READ_ONLY_BIN = {"ls", "pwd", "cat", "head", "tail", "wc", "df", "du", "free", "uptime", "whoami", "hostname", "uname", "date", "which", "stat", "nvidia-smi", "file", "tree"}
_GIT_MUTATING_FLAGS = {"-d", "-D", "-m", "-M", "--delete", "--move", "--set-upstream-to", "-u", "--unset", "--add", "--replace-all", "--unset-all", "set-url", "add", "remove", "rm", "rename", "prune", "--global", "--system"}
_SHELL_META = set(";&|<>`$\n\\")


def is_read_only_command(command: str, extra_allow: list[str] | None = None) -> bool:
    """Conservative: a single simple command from a known read-only set, no shell metacharacters."""
    cmd = (command or "").strip()
    if not cmd or any(ch in _SHELL_META for ch in cmd):
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv:
        return False
    if cmd in (extra_allow or []):
        return True
    prog = argv[0]
    if prog == "git":
        rest = argv[1:]
        while rest[:1] == ["-C"] and len(rest) >= 2:
            rest = rest[2:]
        if not rest or rest[0] not in _READ_ONLY_GIT:
            return False
        if rest[0] in ("branch", "tag", "remote", "config", "worktree") and any(a in _GIT_MUTATING_FLAGS for a in rest[1:]):
            return False
        if rest[0] == "config" and not any(a in ("--get", "--list", "-l", "--get-all") for a in rest[1:]):
            return False
        if rest[0] == "worktree" and rest[1:2] != ["list"]:
            return False
        return True
    if prog == "gh":
        return len(argv) >= 3 and argv[1] in ("repo", "pr", "issue", "run") and argv[2] in ("list", "view", "status")
    return prog in _READ_ONLY_BIN


class ApprovalBroker:
    def __init__(self, emit: Callable[[dict[str, Any]], Awaitable[None]]):
        self.emit = emit
        self._pending: dict[str, dict[str, Any]] = {}

    def pending(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in p.items() if k != "future"} for p in self._pending.values()]

    async def request(self, *, kind: str, node_id: str, command: str, cwd: str, chat_id: str, timeout_sec: float) -> bool:
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        entry = {"requestId": rid, "kind": kind, "nodeId": node_id, "command": command, "cwd": cwd, "chatId": chat_id,
                 "createdAt": time.time(), "timeoutSec": timeout_sec, "future": fut}
        self._pending[rid] = entry
        await self.emit({"type": "approval_request", **{k: v for k, v in entry.items() if k != "future"}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(rid, None)
            await self.emit({"type": "approval_resolved", "requestId": rid})

    def resolve(self, rid: str, approve: bool) -> bool:
        entry = self._pending.get(rid)
        if not entry or entry["future"].done():
            return False
        entry["future"].set_result(bool(approve))
        return True


# ---- Background jobs ----------------------------------------------------------------------------

class JobRunner:
    """Long work (orchestrator turns, agent runs, syncs) runs here instead of inside an HTTP request.

    Progress goes out over /ws/events; state lives in the `jobs` table so a refresh can recover it.
    """

    def __init__(self, conn: sqlite3.Connection, emit: Callable[[dict[str, Any]], Awaitable[None]], node_id: Callable[[], str]):
        self.conn = conn
        self.emit = emit
        self.node_id = node_id
        self.tasks: dict[str, asyncio.Task] = {}

    def _update(self, job_id: str, **cols: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in cols) + ", updated_at=?"
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), time.time(), job_id))
        self.conn.commit()

    async def progress(self, job_id: str, text: str) -> None:
        self._update(job_id, progress_text=text[:500])
        await self.emit({"type": "job_progress", "jobId": job_id, "text": text})

    def start(
        self,
        kind: str,
        title: str,
        work: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        timeout_sec: float = 3600.0,
        payload: dict[str, Any] | None = None,
    ) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = time.time()
        self.conn.execute(
            "INSERT INTO jobs (id, kind, title, status, progress_text, node_id, payload_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, kind, title[:200], "running", "", self.node_id(), json.dumps(payload or {}), now, now),
        )
        self.conn.commit()

        async def _run() -> None:
            await self.emit({"type": "job_started", "jobId": job_id, "kind": kind, "title": title})
            try:
                result = await asyncio.wait_for(work(job_id), timeout=timeout_sec)
                self._update(job_id, status="done", result_json=json.dumps(result or {}, default=str)[:200_000])
                await self.emit({"type": "job_done", "jobId": job_id, "kind": kind, "ok": True})
            except asyncio.CancelledError:
                self._update(job_id, status="cancelled")
                await self.emit({"type": "job_done", "jobId": job_id, "kind": kind, "ok": False, "error": "cancelled"})
                raise
            except asyncio.TimeoutError:
                self._update(job_id, status="error", error=f"timed out after {int(timeout_sec)}s")
                await self.emit({"type": "job_done", "jobId": job_id, "kind": kind, "ok": False, "error": "timeout"})
            except Exception as exc:  # job errors are reported, never crash the server
                self._update(job_id, status="error", error=str(exc)[:2000])
                await self.emit({"type": "job_done", "jobId": job_id, "kind": kind, "ok": False, "error": str(exc)[:500]})
            finally:
                self.tasks.pop(job_id, None)

        self.tasks[job_id] = asyncio.create_task(_run())
        return job_id

    def cancel(self, job_id: str) -> bool:
        t = self.tasks.get(job_id)
        if t and not t.done():
            t.cancel()
            return True
        return False

    def get(self, job_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
        if kind:
            rows = self.conn.execute("SELECT * FROM jobs WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"], "kind": r["kind"], "title": r["title"], "status": r["status"],
            "progressText": r["progress_text"], "nodeId": r["node_id"], "error": r["error"],
            "result": json.loads(r["result_json"] or "{}"), "createdAt": r["created_at"], "updatedAt": r["updated_at"],
        }

    def mark_orphans(self) -> None:
        """Jobs left 'running' by a previous process can't finish; say so instead of spinning forever."""
        self.conn.execute("UPDATE jobs SET status='error', error='server restarted while running' WHERE status='running'")
        self.conn.commit()

    async def shutdown(self) -> None:
        for t in list(self.tasks.values()):
            t.cancel()
        for t in list(self.tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
