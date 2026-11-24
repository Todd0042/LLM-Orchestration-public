#!/usr/bin/env python3
"""Boot a two-node cluster for the browser tests; print its addresses as one JSON line; run until stdin closes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import TOTP_SECRET, Cluster, StatusServer  # noqa: E402


def main() -> None:
    c = Cluster()
    site = StatusServer(200)
    c.configure(desktop_extra={"services": [{"id": "notes-app", "name": "Notes App", "node": "desktop", "health": {"url": site.url},
                                             "publicHost": "notes.example.com"}]})
    d_repo, l_repo = c.make_repo("webapp")
    c.start()
    print(json.dumps({"desktop": c.desktop.url, "tunnel": c.desktop.tunnel, "laptop": c.laptop.url, "totpSecret": TOTP_SECRET,
                      "desktopRepo": str(d_repo), "laptopRepo": str(l_repo), "tmp": str(c.tmp)}), flush=True)
    try:
        sys.stdin.read()
    finally:
        c.stop()


if __name__ == "__main__":
    main()
