"""The orchestrator card: three agent terminals per repo (server/orch_terminal.py).

Reading is allowed wherever you're signed in. Anything that types, starts, hands off or merges is
refused over the internet tunnel: there the terminals are watch-only, and you drive Claude from
your phone through Remote Control instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..orch_terminal import SLOTS, AttachedTerminal, TerminalError
from ._common import no_tunnel, websocket_zone, zone


def build(ctx) -> APIRouter:
    r = APIRouter()
    terms = ctx.orch_terminals

    async def guarded(fn, *args) -> Any:
        try:
            res = fn(*args)
            return await res if asyncio.iscoroutine(res) else res
        except TerminalError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @r.get("/api/orch-terminal/cards")
    async def cards() -> dict[str, Any]:
        return {"cards": await asyncio.to_thread(terms.list_cards)}

    @r.post("/api/orch-terminal/cards")
    async def open_card(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        no_tunnel(request, "Starting agents")
        return await guarded(terms.open_card, str(body.get("repo") or ""))

    @r.delete("/api/orch-terminal/cards/{cid}")
    async def close_card(request: Request, cid: str) -> dict[str, Any]:
        no_tunnel(request, "Closing the agents")
        await guarded(terms.stop_card, cid)
        return {"ok": True}

    @r.post("/api/orch-terminal/cards/{cid}/slots/{slot}/{action}")
    async def slot_action(request: Request, cid: str, slot: str, action: str) -> dict[str, Any]:
        no_tunnel(request, "Starting agents")
        if action == "alacritty":
            if zone(request) != "local":
                raise HTTPException(403, "Alacritty opens on this machine's screen; use it from the machine itself")
            await guarded(terms.open_alacritty, cid, slot)
            return {"ok": True}
        if action not in ("start", "resume", "new"):
            raise HTTPException(404, "Unknown action")
        return await guarded(terms.start_slot, cid, slot, action == "resume", action == "new")

    @r.post("/api/orch-terminal/cards/{cid}/send")
    async def send(request: Request, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        no_tunnel(request, "Typing into the terminals")
        await guarded(terms.send, cid, str(body.get("text") or ""))
        return {"ok": True}

    @r.post("/api/orch-terminal/cards/{cid}/interrupt")
    async def interrupt(request: Request, cid: str) -> dict[str, Any]:
        no_tunnel(request, "Interrupting an agent")
        await guarded(terms.interrupt, cid)
        return {"ok": True}

    @r.post("/api/orch-terminal/cards/{cid}/handoff")
    async def handoff(request: Request, cid: str, body: dict[str, Any]) -> JSONResponse:
        no_tunnel(request, "Handing off")
        await guarded(terms.start_handoff, cid, str(body.get("to") or ""))
        return JSONResponse({"status": "started"}, status_code=202)

    @r.post("/api/orch-terminal/cards/{cid}/merge")
    async def merge(request: Request, cid: str) -> dict[str, Any]:
        no_tunnel(request, "Merging to main")
        return await guarded(terms.merge, cid)

    @r.post("/api/orch-terminal/cards/{cid}/protect")
    async def protect(request: Request, cid: str) -> dict[str, Any]:
        no_tunnel(request, "Changing GitHub branch protection")
        return await guarded(terms.protect, cid)

    @r.get("/api/orch-terminal/cards/{cid}/context")
    async def context(cid: str) -> dict[str, Any]:
        return {"text": await guarded(terms.context_text, cid)}

    @r.post("/api/orch-terminal/hook")
    async def hook(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        if zone(request) != "local":
            raise HTTPException(403, "Hooks report from this machine only")
        ok = terms.hook(str(body.get("card") or ""), str(body.get("slot") or ""), str(body.get("event") or ""),
                        request.headers.get("x-orch-hook", ""))
        if not ok:
            raise HTTPException(403, "Unknown card or secret")
        return {"ok": True}

    @r.websocket("/ws/orch-terminal/{cid}/{slot}")
    async def terminal(ws: WebSocket, cid: str, slot: str) -> None:
        zone_ = websocket_zone(ctx, ws)
        row = ctx.conn.execute("SELECT id FROM orch_cards WHERE id=?", (cid,)).fetchone()
        if zone_ is None or row is None or slot not in SLOTS or not terms.tmux.has(terms._session(cid, slot)):
            await ws.close(code=1008)
            return
        can_input = zone_ != "tunnel"
        client = ws.client.host if ws.client else ""
        ctx.audit.record("terminal_connect", f"{cid}/{slot}", zone=zone_, client=client, input=can_input)
        await ws.accept()
        # Over the tunnel even tmux itself is told this client is read-only.
        term = AttachedTerminal(terms.tmux.attach_argv(terms._session(cid, slot), read_only=not can_input))
        out: asyncio.Queue[bytes | None] = asyncio.Queue()
        await term.start(out.put_nowait)

        async def pump() -> None:
            while (chunk := await out.get()) is not None:
                await ws.send_bytes(chunk)
            await ws.close()

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("t") == "r":
                    term.resize(int(msg.get("c") or 80), int(msg.get("r") or 24))
                elif msg.get("t") == "i" and can_input and terms.can_type(cid, slot, zone_):
                    term.write(str(msg.get("d") or "").encode("utf-8"))
                # anything else (including typing into a terminal without the baton) is dropped
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            pump_task.cancel()
            await term.close()
            ctx.audit.record("terminal_disconnect", f"{cid}/{slot}", zone=zone_, client=client)

    return r
