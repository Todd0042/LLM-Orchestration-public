#!/usr/bin/env python3
"""Set this machine's role, peers, services, backups and failover in settings.json (keeps everything else).

Examples (run on each machine, then restart the service):
  Desktop (hub):  scripts/configure_node.py --id desktop --role hub \
                    --peer laptop=http://192.0.2.20:8790 --services deploy/services.json --backups-to laptop
  Laptop (standby): scripts/configure_node.py --id laptop --role worker \
                    --peer desktop=http://192.0.2.10:8790 --failover-standby desktop
Add a Tailscale URL later with a second --peer for the same id (e.g. --peer laptop=http://laptop.example.ts.net:8790).
--failover-unit and --public-probe default to LLM_ORCH_FAILOVER_UNIT and LLM_ORCH_PUBLIC_PROBE_URL from
llm-orchestration.env.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import SETTINGS_FILE, load_settings, save_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--role", choices=["hub", "worker"], required=True)
    ap.add_argument("--peer", action="append", default=[], help="id=url (repeat for more URLs or peers)")
    ap.add_argument("--services", help="JSON file with this machine's service list")
    ap.add_argument("--backups-to", default=None, help="peer id that stores this machine's database backups")
    ap.add_argument("--failover-standby", default=None, help="primary node id this machine stands in for")
    ap.add_argument("--failover-unit", default=os.environ.get("LLM_ORCH_FAILOVER_UNIT", "llm-orchestration-failover.target"))
    ap.add_argument("--public-probe", default=os.environ.get("LLM_ORCH_PUBLIC_PROBE_URL", ""),
                    help="URL that answers only when the primary machine serves your public address")
    ap.add_argument("--settings", default=str(SETTINGS_FILE))
    a = ap.parse_args()

    path = Path(a.settings)
    s = load_settings(path)
    s["node"].update({"id": a.id, "displayName": a.id.title(), "role": a.role})
    s["node"]["hubUrl"] = ""
    peers: dict[str, dict] = {}
    for spec in a.peer:
        pid, _, url = spec.partition("=")
        p = peers.setdefault(pid, {"id": pid, "name": pid.title(), "enabled": True, "urls": [], "role": "worker" if a.role == "hub" else "hub"})
        p["urls"].append(url.rstrip("/"))
    if peers:
        s["peers"] = [{**p, "url": p["urls"][0]} for p in peers.values()]
        if a.role == "worker":
            s["node"]["hubUrl"] = s["peers"][0]["url"]
    if a.services:
        s["services"] = json.loads(Path(a.services).read_text())
    if a.backups_to is not None:
        s.setdefault("backups", {})["targetNode"] = a.backups_to
    if a.failover_standby:
        s["failover"].update({"enabled": True, "role": "standby", "primaryNode": a.failover_standby,
                              "connectorUnit": a.failover_unit, "publicProbeUrl": a.public_probe})
    save_settings(s, path)
    print(json.dumps({"node": s["node"], "peers": s["peers"], "services": len(s.get("services") or []),
                      "backups": s.get("backups"), "failover": s.get("failover")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
