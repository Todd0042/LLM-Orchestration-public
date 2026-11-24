"""Unified chats: orchestrator (replicated on every node) + AGY/Claude sessions on each node."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..agents import AgentUnavailable
from ..history_readers import list_agy_chats, list_claude_chats, read_agy_chat, read_claude_chat
from ._common import zone


class ChatCreateBody(BaseModel):
    title: str = Field(default="New Agent Thread", max_length=300)
    mode: str = "solo"
    taskType: str = "coding"
    nodeId: str = "auto"
    modelId: str = ""
    workspace: str = ""
    peerConfig: dict[str, Any] = Field(default_factory=dict)


class ChatSendBody(BaseModel):
    content: str = Field(max_length=200_000)
    taskType: str = "coding"
    nodeId: str = "auto"
    modelId: str = ""
    engineModel: str = ""
    effort: str = ""
    mode: str = ""
    fallbackModel: str = ""
    runTools: bool = True


class ChatPatchBody(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    pinned: bool | None = None
    archived: bool | None = None


class RelayStartBody(BaseModel):
    topic: str = Field(max_length=50_000)
    turns: int = Field(default=6, ge=2, le=20)
    nodeA: str = "auto"
    modelA: str = ""
    roleA: str = "Architect & Coder"
    nodeB: str = "auto"
    modelB: str = ""
    roleB: str = "Reviewer & QA"


def _native_detail(ctx, chat_id: str) -> dict[str, Any] | None:
    row = ctx.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        return None
    msgs = ctx.conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY created_at ASC", (chat_id,)).fetchall()
    arts = ctx.conn.execute("SELECT * FROM artifacts WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)).fetchall()
    return {
        "id": row["id"], "source": "orchestration", "nodeId": row["node_id"] or ctx.nodes.local_id, "mode": row["mode"],
        "taskType": row["task_type"], "title": row["title"], "peerConfig": json.loads(row["peer_config_json"] or "{}"),
        "messages": [{"id": m["id"], "role": m["role"], "senderLabel": m["sender_label"], "nodeId": m["node_id"], "modelId": m["model_id"],
                      "content": m["content"], "events": json.loads(m["events_json"] or "[]"), "createdAt": str(int(m["created_at"] * 1000))} for m in msgs],
        "artifacts": [{"id": a["id"], "kind": a["kind"], "title": a["title"], "path": a["path"], "nodeId": a["node_id"], "mimeType": a["mime_type"],
                       "meta": json.loads(a["meta_json"] or "{}")} for a in arts],
    }


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/chats")
    async def list_chats(includePeers: bool = True, includeArchived: bool = False) -> dict[str, Any]:
        s = ctx.settings()
        nid = ctx.nodes.local_id
        rows = ctx.conn.execute(
            "SELECT c.id, c.title, c.workspace, c.mode, c.task_type, c.node_id, c.updated_at, c.pinned, c.archived, "
            "(SELECT substr(content, 1, 140) FROM messages m WHERE m.chat_id = c.id ORDER BY created_at DESC LIMIT 1) AS preview "
            "FROM chats c WHERE (? OR c.archived = 0) ORDER BY c.pinned DESC, c.updated_at DESC LIMIT 200", (1 if includeArchived else 0,)).fetchall()
        items: list[dict[str, Any]] = [{"id": x["id"], "source": "orchestration", "nodeId": x["node_id"] or nid, "mode": x["mode"], "taskType": x["task_type"],
                                        "title": x["title"], "workspace": x["workspace"], "updatedAt": str(int(x["updated_at"] * 1000)),
                                        "pinned": bool(x["pinned"]), "archived": bool(x["archived"]), "preview": x["preview"] or ""} for x in rows]
        items += await asyncio.to_thread(list_agy_chats, Path(s["paths"]["agyDir"]).expanduser(), nid)
        items += await asyncio.to_thread(list_claude_chats, Path(s["paths"]["claudeDir"]).expanduser(), nid)
        if includePeers:
            for pid in ctx.nodes.peer_ids():
                try:
                    data = await ctx.nodes.json(pid, "GET", "/api/chats", params={"includePeers": "false"}, timeout=6.0)
                    items += [c for c in data.get("chats", []) if c.get("source") != "orchestration"]  # those are replicated already
                except Exception:
                    continue
        return {"nodeId": nid, "chats": items}

    @r.get("/api/chats/{chat_node}/{source}/{chat_id}")
    async def chat_detail(chat_node: str, source: str, chat_id: str) -> dict[str, Any]:
        s = ctx.settings()
        if source == "orchestration":
            d = _native_detail(ctx, chat_id)
            if d:
                return d
        if not ctx.nodes.is_local(chat_node):
            try:
                return await ctx.nodes.json(chat_node, "GET", f"/api/chats/{chat_node}/{source}/{chat_id}", timeout=15.0)
            except Exception as exc:
                raise HTTPException(503, f"{chat_node}: {exc}")
        if source == "agy":
            res = await asyncio.to_thread(read_agy_chat, Path(s["paths"]["agyDir"]).expanduser(), chat_id, ctx.nodes.local_id)
        elif source == "claude":
            res = await asyncio.to_thread(read_claude_chat, Path(s["paths"]["claudeDir"]).expanduser(), chat_id, ctx.nodes.local_id)
        else:
            res = None
        if not res:
            raise HTTPException(404, "Chat not found")
        return res

    @r.post("/api/chats/native")
    def create_chat(body: ChatCreateBody) -> dict[str, Any]:
        cid = f"orch_{uuid.uuid4().hex[:12]}"
        now = time.time()
        ws_path = body.workspace.strip() or str(ctx.github_dir())
        ctx.conn.execute("INSERT INTO chats (id, title, workspace, mode, task_type, node_id, peer_config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (cid, body.title, ws_path, body.mode, body.taskType, ctx.nodes.local_id, json.dumps(body.peerConfig), now, now))
        ctx.conn.commit()
        return {"id": cid, "source": "orchestration", "nodeId": ctx.nodes.local_id, "title": body.title, "mode": body.mode, "taskType": body.taskType}

    @r.patch("/api/chats/native/{chat_id}")
    async def patch_chat(chat_id: str, body: ChatPatchBody) -> dict[str, Any]:
        """Rename, pin, or archive (archived chats are hidden, never deleted, so replication stays simple)."""
        sets, vals = [], []
        if body.title is not None and body.title.strip():
            sets.append("title=?"); vals.append(body.title.strip())
        if body.pinned is not None:
            sets.append("pinned=?"); vals.append(int(body.pinned))
        if body.archived is not None:
            sets.append("archived=?"); vals.append(int(body.archived))
        if not sets:
            raise HTTPException(400, "nothing to change")
        sets.append("updated_at=?"); vals.append(time.time())
        cur = ctx.conn.execute(f"UPDATE chats SET {', '.join(sets)} WHERE id=?", (*vals, chat_id))
        ctx.conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "Chat not found")
        await ctx.emit({"type": "chats_changed", "chatId": chat_id})
        return {"ok": True}

    @r.post("/api/chats/native/{chat_id}/send")
    async def send_native(chat_id: str, body: ChatSendBody, request: Request) -> dict[str, Any]:
        try:
            job = ctx.orchestrator.start_turn(chat_id, body.model_dump(), zone(request))
        except KeyError:
            raise HTTPException(404, "Chat not found")
        await ctx.emit({"type": "turn_started", "chatId": chat_id, "jobId": job})
        return {"ok": True, "jobId": job}

    @r.post("/api/chats/{chat_node}/{source}/{chat_id}/send")
    async def send_existing(chat_node: str, source: str, chat_id: str, body: ChatSendBody, request: Request) -> dict[str, Any]:
        if source == "orchestration":
            return await send_native(chat_id, body, request)
        if source not in ("agy", "claude"):
            raise HTTPException(400, f"Unsupported source: {source}")
        session = chat_id.split("__", 1)[1] if (source == "claude" and "__" in chat_id) else chat_id
        detail_cwd = ""
        if source == "claude" and "__" in chat_id:
            detail_cwd = ""  # Claude resumes by session id; its own cwd is recorded in the session
        target = ctx.nodes.local_id if ctx.nodes.is_local(chat_node) else chat_node

        async def work(_jid: str) -> dict[str, Any]:
            try:
                res = await ctx.agents.dispatch(target, "agy-cli" if source == "agy" else "claude-cli", body.content, on_offline="fail",
                                                session_id=session, cwd=detail_cwd, model=body.engineModel or body.modelId,
                                                effort=body.effort, mode=body.mode, fallback_model=body.fallbackModel, chat_id=chat_id, source="inspector")
            except AgentUnavailable as exc:
                await ctx.emit({"type": "chat_error", "chatId": chat_id, "error": str(exc)})
                raise
            finally:
                await ctx.emit({"type": "chat_updated", "chatId": chat_id})
            return {"sessionId": res.get("sessionId"), "executedNode": res.get("executedNode")}

        return {"ok": True, "jobId": ctx.jobs.start("agent_turn", f"{source} turn", work)}

    @r.post("/api/chats/native/{chat_id}/relay")
    async def start_relay(chat_id: str, body: RelayStartBody) -> dict[str, Any]:
        try:
            return {"status": "started", "jobId": ctx.orchestrator.start_relay(chat_id, body.model_dump())}
        except KeyError:
            raise HTTPException(404, "Chat not found")

    @r.post("/api/chats/native/{chat_id}/relay/stop")
    async def stop_relay(chat_id: str) -> dict[str, Any]:
        stopped = 0
        for j in ctx.jobs.recent(50, "relay"):
            if j["status"] == "running" and ctx.jobs.cancel(j["id"]):
                stopped += 1
        return {"status": "stopped", "cancelled": stopped}

    return r
