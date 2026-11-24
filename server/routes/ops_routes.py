"""Services watchdog, backups, failover and alerts."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request

from ._common import require_local


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/services")
    async def services(includePeers: bool = True) -> dict[str, Any]:
        def my_failover() -> dict[str, Any]:
            cfg = ctx.settings().get("failover") or {}
            return {"state": ctx.failover.state if cfg.get("enabled") else "off", "role": cfg.get("role", "none"),
                    "primaryNode": cfg.get("primaryNode", ""), "enabled": bool(cfg.get("enabled"))}

        out = {ctx.nodes.local_id: ctx.monitor.status()}
        failover = {ctx.nodes.local_id: my_failover()}
        backups = {ctx.nodes.local_id: ctx.backups.last}
        if includePeers:
            for pid in ctx.nodes.peer_ids():
                try:
                    data = await ctx.nodes.json(pid, "GET", "/api/services", params={"includePeers": "false"}, timeout=8.0)
                    out.update(data.get("nodes", {}))
                    failover.update(data.get("failoverByNode") or {})
                    backups.update(data.get("backupsByNode") or {})
                except Exception:
                    out[pid] = None
        standby = next((n for n, f in failover.items() if f.get("enabled") and f.get("role") == "standby"), None)
        summary = ({"state": failover[standby]["state"], "enabled": True, "role": "standby", "primaryNode": failover[standby]["primaryNode"], "standbyNode": standby}
                   if standby else {"state": "off", "enabled": False})
        return {"nodes": out, "failover": summary, "failoverByNode": failover, "backups": ctx.backups.last, "backupsByNode": backups}

    @r.get("/api/services/events")
    def service_events(limit: int = 100) -> dict[str, Any]:
        return {"events": ctx.monitor.events(min(limit, 500))}

    @r.post("/api/services/check")
    async def check_now(request: Request) -> dict[str, Any]:
        require_local(request, "Running the watchdog by hand")
        return {"report": await ctx.monitor.tick()}

    @r.post("/api/backups/run")
    async def backups_run(request: Request) -> dict[str, Any]:
        return await ctx.backups.run(force=True)

    @r.post("/api/alerts/test")
    async def alerts_test(request: Request) -> dict[str, Any]:
        ok = await ctx.alerts.send("Test alert", f"Alerts from {ctx.nodes.local_id} are working.", key=f"test:{time.time()}")
        return {"sentToPhone": ok}

    return r
