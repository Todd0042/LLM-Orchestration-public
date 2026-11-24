"""Health, node profile, settings, models, jobs, approvals, audit, token rotation."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..cli_agents import CLAUDE_MODEL_PRESETS, list_agy_models
from ..config import ROOT_DIR, SettingsError
from ..gpu_and_media import discover_node_models
from ..inventory_sync import sync_local_tool_inventory
from ..nodes import local_profile
from ..repo_tracker import hold_counts
from ..security import changed_protected
from ..tools_loader import load_python_tools
from ._common import require_local, zone


class ApprovalBody(BaseModel):
    approve: bool


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Public (peers and the failover probe use it): identity only, no internals for anonymous callers."""
        s = ctx.settings()
        out: dict[str, Any] = {"ok": True, "nodeId": ctx.nodes.local_id, "role": ctx.nodes.role, "displayName": s["node"]["displayName"]}
        if getattr(request.state, "auth", ""):
            out["gpu"] = await ctx.gpu.status()
        return out

    @r.get("/api/node/profile")
    async def node_profile() -> dict[str, Any]:
        return {**await asyncio.to_thread(local_profile, ctx.settings()), "githubDir": str(ctx.github_dir())}

    @r.get("/api/nodes")
    async def nodes_status() -> dict[str, Any]:
        return {"local": ctx.nodes.profiles.get(ctx.nodes.local_id) or {"nodeId": ctx.nodes.local_id},
                "hub": ctx.nodes.hub_id(), "peers": await ctx.nodes.status_all()}

    @r.get("/api/status")
    async def status_summary() -> dict[str, Any]:
        """One cheap call for the status strip: machines, running work, things that need you."""
        peers = await ctx.nodes.status_all()
        running = [j for j in ctx.jobs.recent(50) if j["status"] == "running"]
        attention = ctx.conn.execute(
            "SELECT COUNT(*) AS c FROM repo_handoffs WHERE status NOT IN ('handed_off','already_in_sync') AND created_at > strftime('%s','now') - 86400"
        ).fetchone()["c"]
        local_sites = ctx.monitor.status()
        down = [x["name"] for x in local_sites if x["status"] in ("failing", "gave_up", "restarting")]
        for pid in ctx.nodes.peer_ids():
            if ctx.nodes.cached_online(pid):
                try:
                    data = await ctx.nodes.json(pid, "GET", "/api/services", params={"includePeers": "false"}, timeout=4.0)
                    for lst in (data.get("nodes") or {}).values():
                        down += [x["name"] for x in lst or [] if x["status"] in ("failing", "gave_up", "restarting")]
                except Exception:
                    pass
        hcounts = hold_counts(ctx.tracker_conn)
        return {
            "nodeId": ctx.nodes.local_id,
            "role": ctx.nodes.role,
            "nodes": [{"id": ctx.nodes.local_id, "online": True, "role": ctx.nodes.role, "self": True}]
                     + [{"id": p["id"], "online": p["online"], "role": p.get("role", ""), "self": False} for p in peers],
            "jobsRunning": [{"id": j["id"], "kind": j["kind"], "title": j["title"], "progress": j["progressText"]} for j in running],
            "approvals": len(ctx.approvals.pending()),
            "repoConflicts24h": attention,
            "repoHoldsCount": hcounts["active"],
            "repoNeedsAnswerCount": hcounts["needsAnswer"],
            "sitesDown": down,
            "failover": ctx.failover.state,
            "agentsOut": ctx.availability.snapshot(),
        }

    @r.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {"settings": ctx.settings(), "peersStatus": await ctx.nodes.status_all()}

    @r.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        body = await request.json()
        changed = changed_protected(ctx.settings(), body if isinstance(body, dict) else {})
        if changed:
            require_local(request, ", ".join(changed))
        try:
            new = ctx.update_settings(body)
        except SettingsError as exc:
            raise HTTPException(400, str(exc))
        ctx.audit.record("settings_update", ",".join(sorted(body.keys())), zone=zone(request), protected=changed)
        return {"ok": True, "settings": new}

    @r.post("/api/admin/rotate-token")
    async def rotate_token(request: Request) -> dict[str, Any]:
        require_local(request, "The node token")
        new = __import__("secrets").token_urlsafe(32)
        report = {}
        for pid in ctx.nodes.peer_ids():
            try:
                await ctx.nodes.json(pid, "POST", "/api/internal/token-rotate", json={"token": new}, timeout=10.0)
                report[pid] = "updated"
            except Exception as exc:
                report[pid] = f"NOT updated ({exc}); copy ~/.config/llm-orchestration/node_token there manually"
        ctx.rotate_token(new)
        ctx.audit.record("token_rotated", "", zone=zone(request), peers=report)
        return {"ok": True, "peers": report}

    @r.get("/api/engines/models")
    async def engine_models() -> dict[str, Any]:
        return {"agyModels": await asyncio.to_thread(list_agy_models), "claudePresets": CLAUDE_MODEL_PRESETS,
                "efforts": ["low", "medium", "high", "xhigh", "max"], "agyModes": ["accept-edits", "auto", "plan"]}

    async def _local_models() -> list[dict[str, Any]]:
        s = ctx.settings()
        models = await discover_node_models(s, ctx.nodes.local_id)
        tools = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        for name, t in tools.items():
            if t["category"] == "music" and t["enabled"]:
                models.append({"id": name, "nodeId": ctx.nodes.local_id, "kind": "music", "displayName": name,
                               "strengths": ["Music", "Python Suite"], "fit": "Exclusive GPU Tool", "contextLength": 2048, "status": "plugin"})
        sync_local_tool_inventory(ctx.conn, ctx.nodes.local_id, models, tools, ctx.vault_dir())
        return models

    @r.get("/api/models")
    async def models(localOnly: bool = False) -> dict[str, Any]:
        out = await _local_models()
        if not localOnly:
            for pid in ctx.nodes.peer_ids():
                try:
                    out.extend((await ctx.nodes.json(pid, "GET", "/api/models", params={"localOnly": "true"}, timeout=6.0)).get("models", []))
                except Exception:
                    continue
        return {"nodeId": ctx.nodes.local_id, "models": out}

    @r.get("/api/models/local")
    async def models_local() -> dict[str, Any]:
        return {"models": await _local_models()}

    @r.get("/api/jobs")
    def jobs(kind: str = "") -> dict[str, Any]:
        return {"jobs": ctx.jobs.recent(50, kind)}

    @r.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        j = ctx.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "Job not found")
        return j

    @r.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return {"ok": ctx.jobs.cancel(job_id)}

    @r.get("/api/approvals")
    def approvals() -> dict[str, Any]:
        return {"pending": ctx.approvals.pending()}

    @r.post("/api/approvals/{rid}")
    async def decide(rid: str, body: ApprovalBody, request: Request) -> dict[str, Any]:
        ok = ctx.approvals.resolve(rid, body.approve)
        if not ok:
            raise HTTPException(404, "No pending approval with that id")
        return {"ok": True}

    @r.get("/api/audit")
    def audit(limit: int = 200) -> dict[str, Any]:
        return {"entries": ctx.audit.recent(min(max(limit, 1), 1000))}

    @r.get("/api/mcp/config")
    def mcp_config() -> dict[str, Any]:
        path = ctx.agents.mcp_config_path()
        py = shutil.which("python3") or "/usr/bin/python3"
        return {"configPath": path, "bridgeScript": str(ROOT_DIR / "server" / "mcp_bridge.py"), "claudeCliFlag": f"--mcp-config {path}",
                "agyRegisterCommand": f"agy mcp add llm-orchestration -- {py} {ROOT_DIR / 'server' / 'mcp_bridge.py'}"}

    return r
