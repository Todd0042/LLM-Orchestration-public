"""LLM-Orchestration node daemon: FastAPI app factory, security gate, WebSocket, listeners.

Listeners (one process, one app):
  <bindHost>:<port>        local + LAN clients (LAN needs the node token or a code session)
  127.0.0.1:<tunnelPort>   Cloudflare tunnel only; always requires the 6-digit-code session
  127.0.0.1:<port+2>       maintenance page used by site failover
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from .auth import SESSION_COOKIE_NAME
from .config import CONFIG_DIR, DATA_DIR, ROOT_DIR, load_settings
from .context import AppContext
from .monitor import MAINTENANCE_HTML
from .routes import register_routes
from .routes._common import websocket_zone
from .security import CLIENT_HEADER, STATE_CHANGING

DIST_DIR = ROOT_DIR / "dist"
PUBLIC_STATIC = {"icons", "manifest.webmanifest", "favicon.ico"}
# The orchestrator terminals' hook checks its own per-card secret (server/orch_terminal.py).
PUBLIC_API = {"/api/orch-terminal/hook", "/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/health",
              "/api/auth/passkey/login/options", "/api/auth/passkey/login/verify", "/api/auth/pair/redeem"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _deny(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status_code=status)


def create_app(
    config_dir: Path | None = None,
    db_path: Path | None = None,
    dist_dir: Path | None = None,
    data_dir: Path | None = None,
    start_monitor: bool | None = None,
) -> FastAPI:
    d_dir = data_dir or DATA_DIR
    ctx = AppContext(cfg_dir=config_dir or CONFIG_DIR, data_dir=d_dir, db_path=db_path or (d_dir / "orchestration.db"), dist_dir=dist_dir or DIST_DIR)
    background = (db_path is None) if start_monitor is None else start_monitor

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await ctx.start(background=background)
        yield
        await ctx.stop()

    app = FastAPI(title="LLM-Orchestration Node", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    def _token_ok(value: str) -> bool:
        return bool(value) and hmac.compare_digest(value, ctx.token())

    @app.middleware("http")
    async def security_gate(request: Request, call_next):
        policy = ctx.policy
        host = request.headers.get("host", "")
        origin = request.headers.get("origin", "")
        if not policy.host_ok(host):
            return _deny(421, "Unknown host")
        origin_allowed = policy.origin_ok(origin, host, request.url.scheme)
        if origin and not origin_allowed:
            return _deny(403, "Cross-origin request refused")

        cors = {}
        if origin and origin_allowed:
            cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={**cors, "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
                                                      "Access-Control-Allow-Headers": "Content-Type, X-Node-Token, X-Orch-Client",
                                                      "Access-Control-Max-Age": "600"})

        zone = policy.request_zone(request)
        path = request.url.path
        token_ok = zone != "tunnel" and _token_ok(request.headers.get("x-node-token", ""))
        session_ok = ctx.sessions.get(request.cookies.get(SESSION_COOKIE_NAME, "")) is not None
        everywhere = bool((ctx.settings().get("auth") or {}).get("requireAuthEverywhere"))
        auth = "token" if token_ok else "session" if session_ok else ("local" if zone == "local" and not everywhere else "")
        request.state.zone, request.state.auth = zone, auth

        if path.startswith("/api/"):
            signed_media = path == "/api/media" and ctx.signer.verify(request.query_params.get("path", ""), request.query_params.get("fileNode", "local"),
                                                                      request.query_params.get("exp", ""), request.query_params.get("sig", ""))
            if path not in PUBLIC_API and not auth and not signed_media:
                return _deny(401, "Authentication required")
            if path.startswith("/api/internal/") and not token_ok:
                return _deny(403, "Machine-to-machine endpoint")
            if request.method in STATE_CHANGING and not token_ok and not request.headers.get(CLIENT_HEADER):
                return _deny(403, f"Missing {CLIENT_HEADER} header")

        response = await call_next(request)
        for k, v in cors.items():
            response.headers[k] = v
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not path.startswith("/api/"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    register_routes(app, ctx)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        if websocket_zone(ctx, ws) is None:
            await ws.close(code=1008)
            return
        await ws.accept()
        ctx.emit.clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ctx.emit.clients.discard(ws)

    static_root = ctx.dist_dir
    if (static_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(static_root / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str) -> Response:
        if full_path.startswith(("api/", "ws/")):
            raise HTTPException(404, "Not found")
        # real files from the build (manifest, icons) are served as themselves; everything else is the app
        if full_path.split("/", 1)[0] in PUBLIC_STATIC:
            root = static_root.resolve()
            candidate = (static_root / full_path).resolve()
            if root in candidate.parents and candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=3600"})
        index = static_root / "index.html"
        if not index.is_file():
            return HTMLResponse("<h1>LLM-Orchestration</h1><p>Run `npm run build` for UI assets.</p>", status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def maintenance_app() -> FastAPI:
    m = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @m.get("/{full_path:path}", include_in_schema=False)
    def page(full_path: str) -> Response:
        return HTMLResponse(MAINTENANCE_HTML, status_code=503, headers={"Retry-After": "120", "Cache-Control": "no-store"})

    return m


async def _serve() -> None:
    s = load_settings()
    node = s["node"]
    port = int(node.get("port", 8790))
    app = create_app()
    configs = [
        uvicorn.Config(app, host=str(node.get("bindHost") or "0.0.0.0"), port=port, log_level="info", proxy_headers=False),
        uvicorn.Config(app, host="127.0.0.1", port=int(node.get("tunnelPort", 8791)), log_level="warning", proxy_headers=False, lifespan="off"),
        uvicorn.Config(maintenance_app(), host="127.0.0.1", port=port + 2, log_level="warning", lifespan="off"),
    ]
    servers = [uvicorn.Server(c) for c in configs]
    tasks = [asyncio.create_task(srv.serve()) for srv in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)  # one stopping (e.g. SIGTERM) stops all
    for srv in servers:
        srv.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
