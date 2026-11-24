"""Model routing + OpenAI-compatible completions, taking proper GPU turns from gpu-scheduler.

Routing reads the tool inventory (which node has which model) and node health, never node
names. Every fallback is reported back (`reason`) so the UI can say where a reply ran and why.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, AsyncIterator, Callable
import uuid

import httpx

from .nodes import NodeRegistry, PeerOffline

CLI_MODEL_IDS = {"agy-cli", "claude-cli", "agy", "claude"}
LOCAL_EQUIVALENTS = {"qwen3:30b-a3b": "qwen3-30b-a3b", "qwen3:8b": "qwen3-8b", "dolphin-llama3:8b": "dolphin-llama3:8B",
                     "qwen3-30b-a3b": "qwen3:30b-a3b", "qwen3-8b": "qwen3:8b", "dolphin-llama3:8B": "dolphin-llama3:8b"}


class LlmError(RuntimeError):
    pass


@dataclass
class Route:
    node: str
    model: str
    fallback: bool = False
    reason: str = ""


class GpuTurns:
    """Client for gpu-scheduler. If the scheduler is absent or refuses, the call simply proceeds:
    it never kills other apps' processes to free VRAM."""

    APP_ID = "llm_orchestration"

    def __init__(self, get_settings: Callable[[], dict[str, Any]]):
        self.get_settings = get_settings

    def _cfg(self) -> tuple[bool, str, dict[str, str]]:
        p = self.get_settings()["paths"]
        url = str(p.get("gpuSchedulerUrl") or "").rstrip("/")
        tok_file = Path(str(p.get("gpuSchedulerTokenFile") or (Path.home() / ".config" / "gpu-scheduler" / "token"))).expanduser()
        tok = tok_file.read_text(encoding="utf-8").strip() if tok_file.is_file() else ""
        return bool(p.get("useGpuScheduler", True) and url), url, ({"Authorization": f"Bearer {tok}"} if tok else {})

    async def status(self) -> dict[str, Any]:
        enabled, url, _ = self._cfg()
        if not enabled:
            return {"mode": "standalone", "online": False}
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"{url}/lock/status")
                if r.status_code == 200:
                    return {"mode": "gpu-scheduler", "online": True, "detail": r.json()}
        except Exception:
            pass
        return {"mode": "standalone", "online": False}

    @asynccontextmanager
    async def turn(self, needs: str, job: str, wait_sec: float = 600.0, on_wait: Callable[[str], Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        enabled, url, headers = self._cfg()
        lease: dict[str, Any] = {"acquired": False, "mode": "standalone"}
        hb: asyncio.Task | None = None
        if enabled:
            try:
                async with httpx.AsyncClient(timeout=wait_sec + 10) as c:
                    ticket, deadline = "", asyncio.get_running_loop().time() + wait_sec
                    while True:
                        r = await c.post(f"{url}/lock/acquire", headers=headers, json={
                            "app_id": self.APP_ID, "job_id": job, "needs": needs, "ttl_sec": 120,
                            "wait_timeout_sec": 20, "ticket_id": ticket, "keep_ticket": True})
                        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                        if r.status_code == 200 and body.get("acquired"):
                            lease = {**body, "mode": "gpu-scheduler"}
                            break
                        if r.status_code not in (409,) or asyncio.get_running_loop().time() > deadline:
                            lease = {"acquired": False, "mode": "scheduler-refused", "detail": body.get("detail") or r.text[:200]}
                            break
                        ticket = str(body.get("ticket_id") or "")
                        if on_wait:
                            await on_wait(f"Waiting for a GPU turn (position {body.get('position', '?')})")
                if lease.get("lease_id"):
                    hb = asyncio.create_task(self._heartbeat(url, headers, lease["lease_id"]))
            except Exception as exc:
                lease = {"acquired": False, "mode": "scheduler-unreachable", "detail": str(exc)[:200]}
        try:
            yield lease
        finally:
            if hb:
                hb.cancel()
            if lease.get("lease_id"):
                try:
                    async with httpx.AsyncClient(timeout=5) as c:
                        await c.post(f"{url}/lock/release", headers=headers, json={"lease_id": lease["lease_id"]})
                except Exception:
                    pass

    async def _heartbeat(self, url: str, headers: dict[str, str], lease_id: str) -> None:
        while True:
            await asyncio.sleep(40)
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post(f"{url}/lock/heartbeat", headers=headers, json={"lease_id": lease_id})
            except Exception:
                pass


class LlmRouter:
    def __init__(self, *, nodes: NodeRegistry, get_settings: Callable[[], dict[str, Any]], conn: sqlite3.Connection, gpu: GpuTurns):
        self.nodes = nodes
        self.get_settings = get_settings
        self.conn = conn
        self.gpu = gpu

    def node_models(self, nid: str) -> set[str]:
        rows = self.conn.execute("SELECT tool_id FROM tool_inventory WHERE node_id=?", (nid,)).fetchall()
        return {str(r["tool_id"]).split(":", 1)[1] for r in rows if ":" in str(r["tool_id"])}

    def _pick_local_model(self, model: str) -> str:
        have = self.node_models(self.nodes.local_id)
        if model in have or not have:
            return model
        if LOCAL_EQUIVALENTS.get(model) in have:
            return LOCAL_EQUIVALENTS[model]
        s = self.get_settings()
        for cand in (str(s["engines"]["orchestrator"].get("modelId") or ""), str(s["taskDefaults"]["coding"].get("modelId") or ""), "qwen3-8b", "qwen3:8b"):
            if cand in have:
                return cand
        return model

    async def resolve(self, requested_node: str, task_type: str, model: str = "") -> Route:
        s = self.get_settings()
        tdef = s["taskDefaults"].get(task_type) or s["taskDefaults"]["coding"]
        model = model or str(tdef.get("modelId") or "qwen3-8b")
        node = requested_node if requested_node not in ("", "auto", "local") else str(tdef.get("nodeId") or "auto")
        if node == "auto":
            if model in self.node_models(self.nodes.local_id) or model in CLI_MODEL_IDS:
                return Route(self.nodes.local_id, model)
            for pid in self.nodes.peer_ids():
                if model in self.node_models(pid) and (await self.nodes.check(pid))[0]:
                    return Route(pid, model, reason=f"{model} lives on {pid}")
            return Route(self.nodes.local_id, self._pick_local_model(model))
        if self.nodes.is_local(node):
            return Route(self.nodes.local_id, model)
        ok, _ = await self.nodes.check(node)
        if ok:
            return Route(node, model)
        if model in CLI_MODEL_IDS:
            return Route(self.nodes.local_id, model, True, f"{node} offline; ran on {self.nodes.local_id}")
        local_model = self._pick_local_model(model)
        return Route(self.nodes.local_id, local_model, True, f"{node} offline; used {local_model} on {self.nodes.local_id}")

    def _needs_for(self, base_url: str, model: str) -> str:
        return f"ollama:{model}" if ":11434" in base_url else "llama"

    async def complete_local(self, payload: dict[str, Any], *, job: str = "", on_wait=None) -> dict[str, Any]:
        base = str(self.get_settings()["paths"]["llmBaseUrl"]).rstrip("/")
        async with self.gpu.turn(self._needs_for(base, str(payload.get("model") or "")), job or f"chat_{uuid.uuid4().hex[:6]}", on_wait=on_wait):
            async with httpx.AsyncClient(timeout=300.0) as client:
                try:
                    r = await client.post(f"{base}/chat/completions", json={**payload, "stream": False})
                except httpx.TransportError as exc:
                    raise LlmError(f"Local model server at {base} is unreachable: {exc}")
        if r.status_code >= 400:
            raise LlmError(f"LLM endpoint error ({r.status_code}): {r.text[:300]}")
        return r.json()

    async def complete(self, route: Route, messages: list[dict[str, Any]], temperature: float, max_tokens: int,
                       tools: list[dict[str, Any]] | None = None, *, job: str = "", on_wait=None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": route.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
        if self.nodes.is_local(route.node):
            return await self.complete_local(payload, job=job, on_wait=on_wait)
        try:
            r = await self.nodes.request(route.node, "POST", "/api/internal/chat", json=payload, timeout=600.0)
        except PeerOffline:
            # it went away between routing and the call: fall back here, and say so on the route
            gone = route.node
            route.node, route.model = self.nodes.local_id, self._pick_local_model(route.model)
            route.fallback, route.reason = True, f"{gone} offline; used {route.model} on {route.node}"
            return await self.complete_local({**payload, "model": route.model}, job=job, on_wait=on_wait)
        if r.status_code >= 400:
            raise LlmError(f"{route.node} LLM error ({r.status_code}): {r.text[:300]}")
        return r.json()
