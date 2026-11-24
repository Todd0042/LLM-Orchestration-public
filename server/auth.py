"""RFC 6238 TOTP verifier and brute-force Guard. Sessions live in identity.DeviceSessions."""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
import os
from pathlib import Path
import struct
import threading
import time

from fastapi import Request

SESSION_COOKIE_NAME = "orch_session"


def _hotp(secret_b32: str, counter: int) -> str:
    padded = secret_b32.strip().replace(" ", "").upper()
    pad_len = (-len(padded)) % 8
    key = base64.b32decode(padded + ("=" * pad_len), casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code_int:06d}"


class TotpVerifier:
    """Validates 6-digit RFC 6238 codes using the shared Authenticator secret with replay protection."""

    def __init__(self, secret_path: Path, step_path: Path):
        self.secret_path = secret_path
        self.step_path = step_path
        self._lock = threading.Lock()
        self._last_step = self._load_last_step()

    def _load_last_step(self) -> int:
        try:
            return int(self.step_path.read_text(encoding="utf-8").strip())
        except Exception:
            return -1

    def _save_last_step(self, step: int) -> None:
        self.step_path.parent.mkdir(parents=True, exist_ok=True)
        self.step_path.write_text(str(step), encoding="utf-8")
        os.chmod(self.step_path, 0o600)

    def has_secret(self) -> bool:
        return self.secret_path.is_file()

    def _read_secret(self) -> str:
        if not self.secret_path.is_file():
            raise RuntimeError(f"Missing TOTP secret at {self.secret_path}")
        return self.secret_path.read_text(encoding="utf-8").strip()

    def verify(self, code: str, now: float | None = None) -> bool:
        clean = (code or "").strip().replace(" ", "")
        if len(clean) != 6 or not clean.isdigit():
            return False
        now_val = time.time() if now is None else now
        step = int(now_val // 30)
        secret = self._read_secret()
        expected = _hotp(secret, step)
        if not hmac.compare_digest(expected, clean):
            return False
        with self._lock:
            if step <= self._last_step:
                return False
            self._last_step = step
            self._save_last_step(step)
        return True


class Guard:
    """Sliding-window per-IP and global brute-force lockout."""

    def __init__(self, max_per_ip: int = 5, lockout_sec: int = 900, max_global: int = 30):
        self.max_per_ip = max_per_ip
        self.lockout_sec = lockout_sec
        self.max_global = max_global
        self._fails: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.lockout_sec:
            q.popleft()

    def blocked(self, ip: str, now: float | None = None) -> int:
        now_val = time.time() if now is None else now
        with self._lock:
            self._trim(self._global, now_val)
            if len(self._global) >= self.max_global:
                return int(self.lockout_sec - (now_val - self._global[0])) + 1
            q = self._fails.get(ip)
            if q:
                self._trim(q, now_val)
                if len(q) >= self.max_per_ip:
                    return int(self.lockout_sec - (now_val - q[0])) + 1
        return 0

    def fail(self, ip: str, now: float | None = None) -> None:
        now_val = time.time() if now is None else now
        with self._lock:
            self._fails.setdefault(ip, deque()).append(now_val)
            self._global.append(now_val)
            if len(self._fails) > 10_000:
                for k in list(self._fails)[:5_000]:
                    del self._fails[k]

    def succeed(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def is_tunnel_request(request: Request, trust_cf: bool = True) -> bool:
    """Detect whether the HTTP request arrived via Cloudflare / remote HTTPS proxy."""
    if trust_cf and request.headers.get("cf-connecting-ip"):
        return True
    if request.headers.get("x-forwarded-proto") == "https":
        return True
    if request.headers.get("cf-ray"):
        return True
    return False


def client_ip(request: Request, trust_cf: bool = True) -> str:
    if trust_cf:
        cf = request.headers.get("cf-connecting-ip", "").strip()
        if cf:
            return cf[:64]
    return (request.client.host if request.client else "127.0.0.1")[:64]


def cookie_secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"
