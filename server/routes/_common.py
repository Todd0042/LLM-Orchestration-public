"""Helpers shared by route modules."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, WebSocket

from ..auth import SESSION_COOKIE_NAME


def zone(request: Request) -> str:
    return str(getattr(request.state, "zone", "local"))


def require_local(request: Request, what: str) -> None:
    """Security-sensitive changes can only be made from the machine itself (or by a peer machine)."""
    if getattr(request.state, "auth", "") == "token" or zone(request) == "local":
        return
    raise HTTPException(403, f"{what} can only be changed on the machine itself, not over {zone(request)}")


def no_tunnel(request: Request, what: str) -> None:
    """Over the internet the orchestrator terminals are watch-only."""
    if zone(request) == "tunnel":
        raise HTTPException(403, f"{what} isn't allowed over the internet; the terminals are watch-only there")


def websocket_zone(ctx, ws: WebSocket) -> str | None:
    """The zone of an allowed WebSocket (Host/Origin checked; session, one-time ticket, node token
    or a local client), or None if it must be refused."""
    policy = ctx.policy
    host = ws.headers.get("host", "")
    origin = ws.headers.get("origin", "")
    if not policy.host_ok(host) or (origin and not policy.origin_ok(origin, host, "https" if ws.url.scheme == "wss" else "http")):
        return None
    zone_ = policy.zone_for(ws.scope.get("server"), ws.client.host if ws.client else "", ws.headers)
    session_ok = ctx.sessions.get(ws.cookies.get(SESSION_COOKIE_NAME, "")) is not None
    ticket_ok = ctx.tickets.redeem(ws.query_params.get("ticket", ""))
    token = ws.headers.get("x-node-token", "")
    token_ok = zone_ != "tunnel" and bool(token) and hmac.compare_digest(token, ctx.token())
    everywhere = bool((ctx.settings().get("auth") or {}).get("requireAuthEverywhere"))
    if not (session_ok or ticket_ok or token_ok or (zone_ == "local" and not everywhere)):
        return None
    return zone_
