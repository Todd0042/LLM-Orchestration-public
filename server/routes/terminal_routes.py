"""Terminal (PTY + sudo pop-up), Python tools, media generation, file/media reading."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..gpu_and_media import generate_comfy_media
from ..security import path_allowed, read_roots
from ..tools_loader import load_python_tools
from ._common import zone


class TerminalRunBody(BaseModel):
    command: str = Field(max_length=20_000)
    cwd: str = ""
    nodeId: str = "local"
    chatId: str = ""


class PasswordSubmitBody(BaseModel):
    requestId: str
    nodeId: str = "local"
    password: str | None = None


class MediaGenBody(BaseModel):
    kind: str = "image"
    prompt: str
    modelId: str = ""
    nodeId: str = "auto"
    chatId: str = "media"


class ToolRunBody(BaseModel):
    toolName: str
    args: dict[str, Any] = Field(default_factory=dict)
    chatId: str = ""
    nodeId: str = "local"


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _allowed(path: str) -> Path:
        p = path_allowed(path, read_roots(ctx.settings(), ctx.data_dir))
        if not p:
            raise HTTPException(403, "That path is outside the folders this app may read")
        return p

    @r.post("/api/terminal/run")
    async def run_terminal(body: TerminalRunBody, request: Request) -> dict[str, Any]:
        if not ctx.nodes.is_local(body.nodeId):
            try:
                cwd = await ctx.nodes.path_for_peer(body.nodeId, body.cwd.strip())
                return await ctx.nodes.json(body.nodeId, "POST", "/api/terminal/run", timeout=900.0,
                                            json={"command": body.command, "cwd": cwd, "nodeId": "local", "chatId": body.chatId})
            except Exception as exc:
                raise HTTPException(503, f"{body.nodeId}: {exc}")
        cwd = str(Path(body.cwd.strip()).expanduser()) if body.cwd.strip() else str(Path.home())
        if not Path(cwd).is_dir():
            raise HTTPException(400, f"No folder {cwd} on {ctx.nodes.local_id}")
        repo = ctx.repo_sync.repo_from_path(cwd)
        if repo:
            await ctx.repo_sync.preflight(repo=repo, source="terminal")
        ctx.audit.record("terminal", body.command, zone=zone(request), cwd=cwd)
        res = await ctx.term.run_command(body.command, cwd=cwd, shell=str(ctx.settings()["terminal"].get("shell") or "/usr/bin/bash"), chat_id=body.chatId)
        if body.chatId and ctx.conn.execute("SELECT 1 FROM chats WHERE id=?", (body.chatId,)).fetchone():
            ev = [{"type": "tool", "tool": "terminal", "summary": body.command, "detail": f"Exit {res['exitCode']}\n{res['output'][-3000:]}"}]
            ctx.orchestrator.add_message(body.chatId, "assistant", f"Terminal ({ctx.nodes.local_id})", ctx.nodes.local_id, "bash",
                                         f"`$ {body.command}` exited with code `{res['exitCode']}`.", ev)
            await ctx.emit({"type": "chat_updated", "chatId": body.chatId})
        return res

    @r.post("/api/terminal/password")
    async def submit_password(body: PasswordSubmitBody) -> dict[str, Any]:
        if not ctx.nodes.is_local(body.nodeId):
            return await ctx.nodes.json(body.nodeId, "POST", "/api/terminal/password", timeout=15.0,
                                        json={"requestId": body.requestId, "nodeId": "local", "password": body.password})
        return {"ok": ctx.term.bridge.resolve(body.requestId, body.password)}

    @r.get("/api/tools")
    async def list_tools() -> dict[str, Any]:
        s = ctx.settings()
        reg = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        return {"tools": [{k: t[k] for k in ("name", "category", "description", "gpuNeed", "parameters", "enabled", "path")} for t in reg.values()]}

    @r.post("/api/tools/run")
    async def run_tool(body: ToolRunBody, request: Request) -> dict[str, Any]:
        s = ctx.settings()
        reg = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        tool = reg.get(body.toolName)
        if not tool or not tool["enabled"]:
            raise HTTPException(404, f"Tool {body.toolName} not found or disabled")
        ctx.audit.record("python_tool", body.toolName, zone=zone(request), args=body.args)
        if tool["gpuNeed"] == "exclusive":
            async with ctx.gpu.turn("exclusive", f"tool_{body.toolName}", wait_sec=900):
                res = await asyncio.to_thread(tool["run"], **body.args)
        else:
            res = await asyncio.to_thread(tool["run"], **body.args)
        return {"ok": True, "tool": body.toolName, "result": res}

    @r.post("/api/media/generate")
    async def media_generate(body: MediaGenBody) -> dict[str, Any]:
        s = ctx.settings()
        tdef = s["taskDefaults"].get(body.kind) or s["taskDefaults"]["image"]
        return await generate_comfy_media(ctx.broker, s, body.kind, body.prompt, body.modelId or tdef["modelId"], body.chatId)

    @r.get("/api/file")
    async def read_file(path: str, fileNode: str = "local") -> dict[str, Any]:
        if not ctx.nodes.is_local(fileNode):
            try:
                return await ctx.nodes.json(fileNode, "GET", "/api/file", params={"path": path}, timeout=15.0)
            except Exception as exc:
                raise HTTPException(503, f"{fileNode}: {exc}")
        p = _allowed(path)
        if not p.is_file():
            raise HTTPException(404, f"File not found: {path}")
        if p.stat().st_size > 2_000_000:
            raise HTTPException(413, "File too large for inline viewer")
        return {"path": str(p), "nodeId": ctx.nodes.local_id, "content": p.read_text(encoding="utf-8", errors="replace")}

    @r.get("/api/media/sign")
    def sign_media(path: str, fileNode: str = "local") -> dict[str, Any]:
        sig = ctx.signer.sign(path, fileNode)
        return {"query": f"path={_q(path)}&fileNode={_q(fileNode)}&exp={sig['exp']}&sig={sig['sig']}", **sig}

    @r.get("/api/media")
    async def read_media(path: str, fileNode: str = "local") -> Response:
        if not ctx.nodes.is_local(fileNode):
            try:
                pr = await ctx.nodes.request(fileNode, "GET", "/api/media", params={"path": path, "fileNode": "local"}, timeout=60.0)
            except Exception as exc:
                raise HTTPException(503, f"{fileNode}: {exc}")
            return Response(content=pr.content, status_code=pr.status_code, media_type=pr.headers.get("content-type", "application/octet-stream"))
        p = _allowed(path)
        if not p.is_file():
            raise HTTPException(404, "Media not found")
        mt, _ = mimetypes.guess_type(str(p))
        return FileResponse(p, media_type=mt or "application/octet-stream")

    return r


def _q(v: str) -> str:
    from urllib.parse import quote

    return quote(v, safe="")
