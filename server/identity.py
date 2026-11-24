"""Sign-in for people: trusted-device sessions, passkeys (WebAuthn), and one-time pairing links.

  * A device session is a random token in an HttpOnly cookie; only its SHA-256 is stored.
    It lasts 30 days from last use (sliding), at most 90 days, and can be revoked from any
    signed-in device. Sessions and passkeys replicate, so failover keeps you signed in.
  * Passkeys are discoverable credentials with user verification required (fingerprint/face/PIN).
    Verification is done by the `webauthn` library; only public keys are stored.
  * Pairing links let an already-trusted place (this machine, the other machine, or a signed-in
    device) sign in a new device once, without codes. They are single-use and expire in 10 minutes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

DAY = 86400.0
IDLE_SEC = 30 * DAY
MAX_AGE_SEC = 90 * DAY
TOUCH_EVERY_SEC = 600.0
USER_ID = b"llm-orchestration-owner"  # single-owner app: every passkey belongs to you


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class DeviceSession:
    id: str
    name: str
    method: str
    created_at: float
    last_seen: float
    expires_at: float


class DeviceSessions:
    """Persistent replacement for the old in-memory SessionStore (same core methods)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, name: str = "", method: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.conn.execute(
            "INSERT INTO device_sessions (id, name, method, created_at, last_seen, expires_at, revoked, updated_at) VALUES (?,?,?,?,?,?,0,?)",
            (_hash(token), name[:120], method, now, now, now + IDLE_SEC, now))
        self.conn.commit()
        return token

    def get(self, token: str) -> DeviceSession | None:
        if not token or len(token) > 256:
            return None
        r = self.conn.execute("SELECT * FROM device_sessions WHERE id=?", (_hash(token),)).fetchone()
        now = time.time()
        if not r or r["revoked"] or now >= r["expires_at"] or now - r["created_at"] >= MAX_AGE_SEC:
            return None
        if now - r["last_seen"] >= TOUCH_EVERY_SEC:  # sliding expiry, written at most every 10 min
            new_exp = min(now + IDLE_SEC, r["created_at"] + MAX_AGE_SEC)
            self.conn.execute("UPDATE device_sessions SET last_seen=?, expires_at=?, updated_at=? WHERE id=?", (now, new_exp, now, r["id"]))
            self.conn.commit()
        return DeviceSession(r["id"], r["name"], r["method"], r["created_at"], now, r["expires_at"])

    def remaining(self, s: DeviceSession) -> int:
        return max(0, int(s.expires_at - time.time()))

    def revoke(self, token: str) -> None:
        if token:
            self.revoke_id(_hash(token))

    def revoke_id(self, sid: str) -> bool:
        cur = self.conn.execute("UPDATE device_sessions SET revoked=1, updated_at=? WHERE id=? AND revoked=0", (time.time(), sid))
        self.conn.commit()
        return cur.rowcount > 0

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM device_sessions WHERE revoked=0 AND expires_at>? AND created_at>? ORDER BY last_seen DESC", (now, now - MAX_AGE_SEC)
        ).fetchall()
        return [{"id": r["id"][:16], "name": r["name"], "method": r["method"], "createdAt": r["created_at"],
                 "lastSeen": r["last_seen"], "expiresAt": r["expires_at"]} for r in rows]

    def full_id(self, short: str) -> str | None:
        r = self.conn.execute("SELECT id FROM device_sessions WHERE id LIKE ? AND revoked=0", (short + "%",)).fetchall()
        return r[0]["id"] if len(r) == 1 else None


class PairingLinks:
    def __init__(self, ttl_sec: float = 600.0):
        self.ttl_sec = ttl_sec
        self._links: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._links = {k: v for k, v in self._links.items() if v > now}
            self._links[_hash(token)] = now + self.ttl_sec
        return token

    def redeem(self, token: str) -> bool:
        if not token or len(token) > 128:
            return False
        with self._lock:
            exp = self._links.pop(_hash(token), 0.0)
        return exp > time.time()


class PasskeyError(ValueError):
    pass


class Passkeys:
    def __init__(self, conn: sqlite3.Connection, rp_name: str = "LLM-Orchestration"):
        self.conn = conn
        self.rp_name = rp_name
        self._challenges: dict[str, tuple[bytes, str, float]] = {}
        self._lock = threading.Lock()

    # ---- challenges -------------------------------------------------------------------------------
    def _remember(self, challenge: bytes, purpose: str) -> str:
        cid = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            self._challenges = {k: v for k, v in self._challenges.items() if v[2] > now}
            self._challenges[cid] = (challenge, purpose, now + 300)
        return cid

    def _take(self, cid: str, purpose: str) -> bytes:
        with self._lock:
            entry = self._challenges.pop(cid or "", None)
        if not entry or entry[1] != purpose or entry[2] < time.time():
            raise PasskeyError("This sign-in attempt expired. Try again.")
        return entry[0]

    # ---- storage ----------------------------------------------------------------------------------
    def active(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM passkeys WHERE revoked=0 ORDER BY created_at").fetchall()

    def count(self) -> int:
        return len(self.active())

    def list(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "name": r["name"], "createdAt": r["created_at"], "lastUsedAt": r["last_used_at"]} for r in self.active()]

    def revoke(self, pid: str) -> bool:
        cur = self.conn.execute("UPDATE passkeys SET revoked=1, updated_at=? WHERE id=? AND revoked=0", (time.time(), pid))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- registration (only from an already-signed-in place) ----------------------------------------
    def registration_options(self, rp_id: str) -> dict[str, Any]:
        opts = generate_registration_options(
            rp_id=rp_id, rp_name=self.rp_name, user_id=USER_ID, user_name="owner", user_display_name="Owner",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=_unb64u(r["id"])) for r in self.active()],
        )
        return {"challengeId": self._remember(opts.challenge, f"register:{rp_id}"), "options": json.loads(options_to_json(opts))}

    def register(self, rp_id: str, origins: list[str], challenge_id: str, credential: dict[str, Any], name: str) -> dict[str, Any]:
        challenge = self._take(challenge_id, f"register:{rp_id}")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                             expected_origin=origins, require_user_verification=True)
        except Exception as exc:  # library raises several specific types; all mean "not accepted"
            raise PasskeyError(f"Passkey not accepted: {exc}")
        pid = _b64u(v.credential_id)
        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO passkeys (id, public_key, sign_count, name, created_at, last_used_at, revoked, updated_at) VALUES (?,?,?,?,?,?,0,?)",
            (pid, _b64u(v.credential_public_key), v.sign_count, (name or "Passkey")[:80], now, now, now))
        self.conn.commit()
        return {"id": pid, "name": name}

    # ---- sign-in ----------------------------------------------------------------------------------
    def login_options(self, rp_id: str) -> dict[str, Any]:
        opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        return {"challengeId": self._remember(opts.challenge, f"login:{rp_id}"), "options": json.loads(options_to_json(opts))}

    def login(self, rp_id: str, origins: list[str], challenge_id: str, credential: dict[str, Any]) -> dict[str, Any]:
        challenge = self._take(challenge_id, f"login:{rp_id}")
        pid = str(credential.get("rawId") or credential.get("id") or "")
        row = self.conn.execute("SELECT * FROM passkeys WHERE id=? AND revoked=0", (pid,)).fetchone()
        if not row:
            raise PasskeyError("That passkey isn't registered here (or was removed).")
        try:
            v = verify_authentication_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                               expected_origin=origins, credential_public_key=_unb64u(row["public_key"]),
                                               credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except Exception as exc:
            raise PasskeyError(f"Passkey not accepted: {exc}")
        now = time.time()
        self.conn.execute("UPDATE passkeys SET sign_count=?, last_used_at=?, updated_at=? WHERE id=?", (v.new_sign_count, now, now, pid))
        self.conn.commit()
        return {"id": pid, "name": row["name"]}
