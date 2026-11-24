#!/usr/bin/env python3
"""Create the authenticator secret for the 6-digit sign-in code, and show it as a QR code.

  scripts/new-totp.py            create one if this machine has none (safe to re-run)
  scripts/new-totp.py --rotate   replace it (every authenticator app must scan the new code)

The secret is written to <auth.totpStateDir>/totp_secret (default ~/.local/share/llm-orchestration),
readable only by you. Scan the QR code (or type the key) into any authenticator app.
Run it on each machine that people sign in to, or copy the same secret file to both.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotate", action="store_true", help="replace an existing secret")
    ap.add_argument("--issuer", default="LLM-Orchestration", help="name your authenticator app shows")
    a = ap.parse_args()

    s = load_settings()
    folder = Path(str((s.get("auth") or {}).get("totpStateDir") or "")).expanduser()
    if not str(folder):
        print("auth.totpStateDir is not set", file=sys.stderr)
        return 1
    path = folder / "totp_secret"
    if path.is_file() and not a.rotate:
        print(f"This machine already has an authenticator secret ({path}). Use --rotate to replace it.")
        return 0

    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret + "\n")
    os.replace(tmp, path)
    (folder / "last_totp_step").unlink(missing_ok=True)

    label = f"{a.issuer}:{s['node'].get('id') or socket.gethostname()}"
    params = urlencode({"secret": secret, "issuer": a.issuer, "digits": 6, "period": 30})
    uri = f"otpauth://totp/{quote(label)}?" + params
    print(f"Authenticator secret saved to {path} (only you can read it).\n")
    if shutil.which("qrencode"):
        print("Scan this with your authenticator app:\n")
        subprocess.run(["qrencode", "-t", "ANSIUTF8", uri], check=False)
    else:
        print("Install `qrencode` to see a QR code here; or add this to your authenticator app:\n")
        print(f"  {uri}\n")
    print(f"Manual entry key: {secret}")
    print("\nThis key is shown once. Don't paste it anywhere else.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
