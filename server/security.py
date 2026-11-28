"""Request trust zones, origin/host/CSRF policy, file-read allowlist, signed media URLs, WS tickets.

Zones (decided per request, never relaxed by settings):
  tunnel  -> arrived on the dedicated tunnel listener port, or carries Cloudflare headers.
             Only a 6-digit-code session cookie authenticates. The node token is NOT accepted,
             so a leaked token is useless from the internet.
  lan     -> any non-loopback client. Node token (machines) or session cookie (you, after the code).
  local   -> loopback client on the main port. Trusted, but still subject to origin/host/CSRF
             checks so websites open in a local browser can't drive the API.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Any, Iterable

from fastapi import Request

TAURI_ORIGINS = {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
DEV_ORIGINS = {"http://localhost:5174", "http://127.0.0.1:5174"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
CLIENT_HEADER = "x-orch-client"
CF_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-visitor", "cdn-loop")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def split_host(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class SecurityPolicy:
    def __init__(self, get_settings, tunnel_port: int):
        self.get_settings = get_settings
        self.tunnel_port = tunnel_port
        self._machine_names = {n for n in {socket.gethostname().lower(), socket.getfqdn().lower()} if n}

    # ---- settings-derived lists ---------------------------------------------------------------
    def _sec(self) -> dict[str, Any]:
        return self.get_settings().get("security") or {}

    def public_hosts(self) -> set[str]:
        hosts = {str(h).lower() for h in self._sec().get("publicHosts") or []}
        return {h for h in hosts if h}

    def allowed_hosts(self) -> set[str]:
        extra = {str(h).lower() for h in self._sec().get("allowedHosts") or []}
        base = set(LOOPBACK_HOSTS) | self._machine_names | {f"{n}.local" for n in self._machine_names}
        return base | extra | self.public_hosts()

    def host_ok(self, host_header: str) -> bool:
        host = split_host(host_header)
        if not host:
            return False
        if _is_ip_literal(host):
            return True  # DNS rebinding needs a hostname; raw IPs are safe to accept
        if host in self.allowed_hosts():
            return True
        suffixes = [str(s).lower() for s in self._sec().get("allowedHostSuffixes") or [".ts.net"]]
        return any(host.endswith(s) and s.startswith(".") for s in suffixes)

    def origin_ok(self, origin: str, host_header: str, scheme: str) -> bool:
        if not origin or origin == "null":
            return origin == ""
        o = origin.rstrip("/").lower()
        if o in TAURI_ORIGINS:
            return True
        if o in DEV_ORIGINS and self._sec().get("allowDevOrigins", True):
            return True
        if o in {f"{scheme}://{host_header.lower()}", f"http://{host_header.lower()}", f"https://{host_header.lower()}"}:
            return self.host_ok(host_header)
        if o in {f"https://{h}" for h in self.public_hosts()}:
            return True
        extra = {str(x).rstrip("/").lower() for x in self._sec().get("allowedOrigins") or []}
        return o in extra

    # ---- zone ----------------------------------------------------------------------------------
    def zone_for(self, scope_server: tuple[str, int] | None, client_host: str, headers: Any) -> str:
        port = int(scope_server[1]) if scope_server else 0
        if port == self.tunnel_port:
            return "tunnel"
        if any(headers.get(h) for h in CF_HEADERS):
            return "tunnel"
        if headers.get("x-forwarded-proto") == "https" or headers.get("x-forwarded-for"):
            return "tunnel"
        if client_host in ("127.0.0.1", "::1"):
            return "local"
        return "lan"

    def request_zone(self, request: Request) -> str:
        host = request.client.host if request.client else ""
        return self.zone_for(request.scope.get("server"), host, request.headers)


# ---- File read allowlist ------------------------------------------------------------------------

def read_roots(settings: dict[str, Any], data_dir: Path) -> list[Path]:
    p = settings.get("paths") or {}
    roots = [p.get("githubDir"), p.get("vaultDir"), p.get("agyDir"), p.get("claudeDir"), str(data_dir / "artifacts"), p.get("backupsDir")]
    roots += list((settings.get("security") or {}).get("extraReadRoots") or [])
    out: list[Path] = []
    for r in roots:
        if r:
            try:
                out.append(Path(str(r)).expanduser().resolve())
            except OSError:
                continue
    return out


def path_allowed(path: str | Path, roots: Iterable[Path]) -> Path | None:
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for root in roots:
        if target == root or root in target.parents:
            return target
    return None


# ---- Signed media URLs (replaces ?token=<master token>) -----------------------------------------

class MediaSigner:
    def __init__(self, key: str, ttl_sec: int = 3600):
        self._key = hashlib.sha256(("media:" + key).encode()).digest()
        self.ttl_sec = ttl_sec

    def _mac(self, path: str, node: str, exp: int) -> str:
        return hmac.new(self._key, f"{path}\0{node}\0{exp}".encode(), hashlib.sha256).hexdigest()[:40]

    def sign(self, path: str, node: str) -> dict[str, str]:
        exp = int(time.time()) + self.ttl_sec
        return {"exp": str(exp), "sig": self._mac(path, node, exp)}

    def verify(self, path: str, node: str, exp: str, sig: str) -> bool:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_i < time.time():
            return False
        return hmac.compare_digest(self._mac(path, node, exp_i), sig or "")


# ---- One-time WebSocket tickets (replaces ?token= on /ws/events) --------------------------------

class TicketStore:
    def __init__(self, ttl_sec: float = 60.0):
        self.ttl_sec = ttl_sec
        self._tickets: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        t = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._tickets = {k: v for k, v in self._tickets.items() if v > now}
            self._tickets[t] = now + self.ttl_sec
        return t

    def redeem(self, t: str) -> bool:
        if not t:
            return False
        with self._lock:
            exp = self._tickets.pop(t, 0.0)
        return exp > time.time()


# ---- Protected settings: only editable from the machine itself ----------------------------------

PROTECTED_PATHS = (
    ("auth",),
    ("security",),
    ("node",),
    ("peers",),
    ("paths", "toolsDir"),
    ("paths", "agyDir"),
    ("paths", "claudeDir"),
    ("paths", "backupsDir"),
    # opencode runs with --auto against this server, so the URL decides what it does
    ("paths", "llmBaseUrl"),
    ("terminal", "shell"),
    ("terminal", "askApprovalBeforeRun"),
    ("terminal", "readOnlyAllowlist"),
    ("engines", "agy", "dangerouslySkipPermissions"),
    ("engines", "claude", "dangerouslySkipPermissions"),
    ("services",),
    ("alerts",),
    ("failover",),
    ("backups",),
    ("repoSync",),
    # which programs the orchestrator terminals run, with permission checks skipped
    ("orchTerminal",),
)


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def changed_protected(current: dict[str, Any], proposed: dict[str, Any]) -> list[str]:
    return [".".join(p) for p in PROTECTED_PATHS if _dig(proposed, p) is not None and _dig(proposed, p) != _dig(current, p)]
