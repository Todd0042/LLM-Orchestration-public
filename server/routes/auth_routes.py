"""Signing in: passkeys (primary), pairing links for new devices, the 6-digit code (fallback),
trusted-device sessions, the devices list, and one-time WebSocket tickets."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import SESSION_COOKIE_NAME, cookie_secure
from ..identity import IDLE_SEC, PasskeyError
from ..security import split_host
from ._common import zone


class LoginBody(BaseModel):
    code: str = Field(max_length=16)


class PasskeyVerifyBody(BaseModel):
    challengeId: str = Field(max_length=64)
    credential: dict[str, Any]
    name: str = Field(default="", max_length=80)


class PairBody(BaseModel):
    token: str = Field(max_length=128)
    name: str = Field(default="", max_length=80)


class RevokeBody(BaseModel):
    kind: str = Field(pattern="^(passkey|session)$")
    id: str = Field(max_length=200)


def _client_ip(request: Request) -> str:
    if zone(request) == "tunnel":  # only trust Cloudflare's client IP header on the tunnel
        cf = request.headers.get("cf-connecting-ip", "").strip()
        if cf:
            return cf[:64]
    return (request.client.host if request.client else "unknown")[:64]


def device_name(request: Request, given: str = "") -> str:
    if given.strip():
        return given.strip()[:80]
    ua = request.headers.get("user-agent", "")
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                               ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "Device")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "")
    return f"{os_} {br}".strip()


def build(ctx) -> APIRouter:
    r = APIRouter()

    def rp_for(request: Request) -> tuple[str, list[str]] | None:
        """Passkeys belong to one site name: the public HTTPS address, or localhost for development."""
        host_header = request.headers.get("host", "")
        host = split_host(host_header)
        if host in ctx.policy.public_hosts():
            return host, [f"https://{host}"]
        if host == "localhost":
            return "localhost", [f"http://{host_header}", f"https://{host_header}"]
        return None

    def require_rp(request: Request) -> tuple[str, list[str]]:
        rp = rp_for(request)
        if not rp:
            hosts = sorted(ctx.policy.public_hosts())
            raise HTTPException(400, f"Passkeys work on https://{hosts[0] if hosts else 'your public address'}, not on this address.")
        return rp

    def start_session(request: Request, response: Response, method: str, name: str = "") -> dict[str, Any]:
        token = ctx.sessions.create(name=device_name(request, name), method=method)
        secure = cookie_secure(request) or zone(request) == "tunnel" or split_host(request.headers.get("host", "")) in ctx.policy.public_hosts()
        response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=secure, samesite="strict" if secure else "lax",
                            path="/", max_age=int(IDLE_SEC))
        ctx.audit.record("login", method, zone=zone(request), client=_client_ip(request), device=device_name(request, name))
        return {"ok": True, "remainingSec": int(IDLE_SEC)}

    def guarded(request: Request) -> str:
        ip = _client_ip(request)
        wait = ctx.guard.blocked(ip)
        if wait:
            raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
        return ip

    @r.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        rp = rp_for(request)
        common = {"zone": zone(request), "passkeyCapable": bool(rp), "hasPasskeys": ctx.passkeys.count() > 0}
        auth = getattr(request.state, "auth", "")
        if auth in ("local", "token"):
            return {"authenticated": True, "authRequired": False, **common}
        sess = ctx.sessions.get(request.cookies.get(SESSION_COOKIE_NAME, ""))
        if not sess:
            return {"authenticated": False, "authRequired": True, "hasSecret": ctx.totp.has_secret(), **common}
        return {"authenticated": True, "authRequired": True, "remainingSec": ctx.sessions.remaining(sess),
                "sessionId": sess.id[:16], "method": sess.method, **common}

    # ---- passkeys ---------------------------------------------------------------------------------
    @r.post("/api/auth/passkey/login/options")
    def passkey_login_options(request: Request) -> dict[str, Any]:
        rp_id, _ = require_rp(request)
        if not ctx.passkeys.count():
            raise HTTPException(404, "No passkeys yet. Pair this device from one of your machines first.")
        return ctx.passkeys.login_options(rp_id)

    @r.post("/api/auth/passkey/login/verify")
    def passkey_login_verify(body: PasskeyVerifyBody, request: Request, response: Response) -> dict[str, Any]:
        ip = guarded(request)
        rp_id, origins = require_rp(request)
        try:
            key = ctx.passkeys.login(rp_id, origins, body.challengeId, body.credential)
        except PasskeyError as exc:
            ctx.guard.fail(ip)
            ctx.audit.record("login_failed", "passkey", zone=zone(request), client=ip, error=str(exc)[:200])
            raise HTTPException(401, str(exc))
        ctx.guard.succeed(ip)
        return start_session(request, response, "passkey", f"{device_name(request)} · {key['name']}")

    @r.post("/api/auth/passkey/register/options")
    def passkey_register_options(request: Request) -> dict[str, Any]:
        rp_id, _ = require_rp(request)
        return ctx.passkeys.registration_options(rp_id)

    @r.post("/api/auth/passkey/register/verify")
    def passkey_register_verify(body: PasskeyVerifyBody, request: Request) -> dict[str, Any]:
        rp_id, origins = require_rp(request)
        try:
            out = ctx.passkeys.register(rp_id, origins, body.challengeId, body.credential, device_name(request, body.name))
        except PasskeyError as exc:
            raise HTTPException(400, str(exc))
        ctx.audit.record("passkey_added", out["name"], zone=zone(request), client=_client_ip(request))
        return {"ok": True, **out}

    # ---- pairing a new device -----------------------------------------------------------------------
    @r.post("/api/auth/pair/start")
    def pair_start(request: Request) -> dict[str, Any]:
        token = ctx.pairing.issue()
        hosts = sorted(ctx.policy.public_hosts())
        base = f"https://{hosts[0]}" if hosts else f"{request.url.scheme}://{request.headers.get('host', '')}"
        ctx.audit.record("pairing_link", "", zone=zone(request), client=_client_ip(request))
        return {"url": f"{base}/#pair={token}", "token": token, "expiresInSec": int(ctx.pairing.ttl_sec)}

    @r.post("/api/auth/pair/redeem")
    def pair_redeem(body: PairBody, request: Request, response: Response) -> dict[str, Any]:
        ip = guarded(request)
        if not ctx.pairing.redeem(body.token):
            ctx.guard.fail(ip)
            ctx.audit.record("login_failed", "pairing", zone=zone(request), client=ip)
            raise HTTPException(401, "That pairing link is used up or expired. Make a new one.")
        ctx.guard.succeed(ip)
        return start_session(request, response, "pairing", body.name)

    # ---- 6-digit code (fallback) ----------------------------------------------------------------------
    @r.post("/api/auth/login")
    def auth_login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        ip = guarded(request)
        if not ctx.totp.has_secret():
            raise HTTPException(503, "Sign-in with a code isn't set up on this machine yet. "
                                     "Run scripts/new-totp.py on it, then add the code to your authenticator app.")
        try:
            ok = ctx.totp.verify(body.code)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc))
        if not ok:
            ctx.guard.fail(ip)
            ctx.audit.record("login_failed", "code", zone=zone(request), client=ip)
            raise HTTPException(401, "Invalid or already-used 6-digit code. Wait for the next code.")
        ctx.guard.succeed(ip)
        return start_session(request, response, "code")

    @r.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        ctx.sessions.revoke(request.cookies.get(SESSION_COOKIE_NAME, ""))
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"ok": True}

    # ---- devices ---------------------------------------------------------------------------------------
    @r.get("/api/auth/devices")
    def devices(request: Request) -> dict[str, Any]:
        cur = ctx.sessions.get(request.cookies.get(SESSION_COOKIE_NAME, ""))
        return {"passkeys": ctx.passkeys.list(), "sessions": ctx.sessions.list(), "currentSessionId": cur.id[:16] if cur else "",
                "passkeyCapable": bool(rp_for(request))}

    @r.post("/api/auth/devices/revoke")
    def revoke(body: RevokeBody, request: Request) -> dict[str, Any]:
        if body.kind == "passkey":
            ok = ctx.passkeys.revoke(body.id)
        else:
            full = ctx.sessions.full_id(body.id) if re.fullmatch(r"[0-9a-f]{16,64}", body.id) else None
            ok = bool(full) and ctx.sessions.revoke_id(full)
        if not ok:
            raise HTTPException(404, "Nothing to remove with that id")
        ctx.audit.record("device_revoked", body.kind, zone=zone(request), client=_client_ip(request), id=body.id[:16])
        return {"ok": True}

    @r.post("/api/ws/ticket")
    def ws_ticket() -> dict[str, Any]:
        return {"ticket": ctx.tickets.issue(), "ttlSec": int(ctx.tickets.ttl_sec)}

    return r
