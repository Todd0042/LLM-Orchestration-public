"""Machine-to-machine endpoints (node token required; enforced in the security gate)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..agents import AgentUnavailable
from ..inventory_sync import resolve_vault_path
from ..llm import LlmError
from ..monitor import store_backup
from ..replication import STREAMS, export_rows
from ..repo_ops import RepoOpError
from ..repo_tracker import export_local_tracker_payload, set_repo_hold


class McpPeerAskBody(BaseModel):
    targetNode: str = "auto"
    agent: str = "claude-cli"
    prompt: str
    cwd: str = ""


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.post("/api/internal/chat")
    async def internal_chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        try:
            return await ctx.llm.complete_local(body, job=f"peer_{str(body.get('model') or '')[:40]}")
        except LlmError as exc:
            raise HTTPException(502, str(exc))

    @r.post("/api/internal/agent_turn")
    async def internal_agent_turn(request: Request) -> dict[str, Any]:
        b = await request.json()
        try:
            return await ctx.agents.run_local(
                str(b.get("agent") or "agy-cli"), str(b.get("prompt") or ""), session_id=str(b.get("sessionId") or ""),
                cwd=str(b.get("cwd") or ""), model=str(b.get("model") or ""), effort=str(b.get("effort") or ""),
                mode=str(b.get("mode") or ""), fallback_model=str(b.get("fallbackModel") or ""), chat_id=str(b.get("chatId") or ""),
                source=str(b.get("source") or "peer"), system_prompt=str(b.get("systemPrompt") or ""))
        except AgentUnavailable as exc:
            raise HTTPException(409, str(exc))

    @r.post("/api/internal/repo/{op}")
    async def repo_op(op: str, request: Request) -> Response:
        b = await request.json()
        bundle = base64.b64decode(b.pop("bundleB64", "") or "")
        try:
            out = await ctx.repo_sync.local_op(op, b, bundle)
        except RepoOpError as exc:
            raise HTTPException(409, str(exc))
        if op == "bundle":
            return Response(content=out, media_type="application/octet-stream")
        from fastapi.responses import JSONResponse

        return JSONResponse(out if out is not None else {"ok": True})

    @r.get("/api/internal/repo-tracker")
    async def internal_repo_tracker(refresh: bool = False) -> dict[str, Any]:
        if refresh:
            await ctx.rescan_tracker(check_remote=True)
        return export_local_tracker_payload(ctx.tracker_conn, ctx.nodes.local_id)

    @r.post("/api/internal/repo-holds")
    async def internal_repo_holds(request: Request) -> dict[str, Any]:
        b = await request.json()
        hold = set_repo_hold(
            ctx.tracker_conn,
            str(b.get("repoName") or ""),
            str(b.get("state") or "active"),
            set_by=str(b.get("setBy") or "peer"),
            note=str(b.get("note") or ""),
            data_dir=ctx.data_dir,
            vault_dir=ctx.vault_dir(),
        )
        return {"ok": True, "hold": hold}

    @r.get("/api/internal/replicate/pull")
    def replicate_pull(stream: str, after: int = 0) -> dict[str, Any]:
        if stream not in STREAMS:
            raise HTTPException(400, "unknown stream")
        return export_rows(ctx.conn, stream, after)

    @r.get("/api/internal/vault/manifest")
    async def vault_manifest() -> dict[str, Any]:
        return {"files": await asyncio.to_thread(ctx.replicator.vault_index.manifest, ctx.vault_dir())}

    @r.get("/api/internal/vault/raw")
    def vault_raw(relPath: str) -> Response:
        try:
            p = resolve_vault_path(ctx.vault_dir(), relPath)
        except ValueError as exc:
            raise HTTPException(403, str(exc))
        if not p.is_file():
            raise HTTPException(404, "missing")
        return FileResponse(p, media_type="application/octet-stream")

    @r.post("/api/internal/backups/upload")
    async def backup_upload(request: Request, node: str, service: str, name: str) -> dict[str, Any]:
        data = await request.body()
        if len(data) > 2 * 1024 ** 3:
            raise HTTPException(413, "backup too large")
        keep = int((ctx.settings().get("backups") or {}).get("keep", 14))
        dest = await asyncio.to_thread(store_backup, ctx.backups_dir(), node, service, name, data, keep)
        return {"ok": True, "path": str(dest)}

    @r.post("/api/internal/token-rotate")
    async def token_rotate(request: Request) -> dict[str, Any]:
        b = await request.json()
        tok = str(b.get("token") or "")
        if len(tok) < 32:
            raise HTTPException(400, "token too short")
        ctx.rotate_token(tok)
        ctx.audit.record("token_rotated_by_peer", "")
        return {"ok": True}

    @r.post("/api/internal/cloudflared-credential")
    async def cloudflared_credential(request: Request) -> dict[str, Any]:
        """Install a tunnel credentials file for the failover connector. Narrow on purpose:
        only ~/.cloudflared/<tunnel-uuid>.json, mode 0600, contents never logged."""
        import json as _json
        import os
        from pathlib import Path
        import re

        b = await request.json()
        name = str(b.get("name") or "")
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json", name):
            raise HTTPException(400, "name must be <tunnel-uuid>.json")
        raw = base64.b64decode(str(b.get("contentB64") or ""), validate=True)
        try:
            parsed = _json.loads(raw)
        except ValueError:
            raise HTTPException(400, "not a credentials JSON")
        if not isinstance(parsed, dict) or "TunnelSecret" not in parsed:
            raise HTTPException(400, "not a cloudflared credentials file")
        d = Path.home() / ".cloudflared"
        d.mkdir(mode=0o700, exist_ok=True)
        dest = d / name
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.chmod(tmp, 0o600)
        tmp.replace(dest)
        ctx.audit.record("cloudflared_credential_installed", name)
        return {"ok": True, "path": str(dest)}

    @r.get("/api/internal/orch-tools")
    def orch_tool_list() -> dict[str, Any]:
        """Orchestrator tool schemas, for the MCP bridge (so Claude/AGY get the same tools as the local loop)."""
        from ..orch_tools import builtin_tool_schemas

        return {"tools": [t["function"] for t in builtin_tool_schemas()]}

    @r.post("/api/internal/orch-tool")
    async def orch_tool_call(request: Request) -> dict[str, Any]:
        b = await request.json()
        name = str(b.get("name") or "")
        if not name.startswith("orch_"):
            raise HTTPException(400, "not an orchestrator tool")
        result = await ctx.orchestrator.call_tool(name, dict(b.get("arguments") or {}), chat_id=str(b.get("chatId") or ""), zone="agent")
        return {"result": result}

    @r.post("/api/mcp/peer-ask")
    async def mcp_peer_ask(body: McpPeerAskBody) -> dict[str, Any]:
        try:
            return await ctx.agents.dispatch(body.targetNode, body.agent, body.prompt, cwd=body.cwd or str(ctx.github_dir()),
                                             on_offline="fail" if body.targetNode not in ("", "auto") else "local", source="mcp")
        except AgentUnavailable as exc:
            raise HTTPException(409, str(exc))

    return r
