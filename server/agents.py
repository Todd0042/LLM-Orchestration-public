"""Dispatch AGY / Claude Code turns to the right node, with explicit (never silent) fallback.

Paths are mapped between nodes by repo: ~/GitHub/X on one machine is ~/Documents/GitHub/X
on the other, so a turn never runs in a folder that doesn't exist on the executing node.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from typing import Any, Awaitable, Callable

from .cli_agents import run_agy_turn, run_claude_turn
from .config import ROOT_DIR
from .nodes import NodeRegistry, PeerOffline

AGENT_IDS = {"agy": "agy-cli", "agy-cli": "agy-cli", "claude": "claude-cli", "claude-cli": "claude-cli"}


class AgentUnavailable(RuntimeError):
    pass


class AgentDispatcher:
    def __init__(
        self,
        *,
        nodes: NodeRegistry,
        get_settings: Callable[[], dict[str, Any]],
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        askpass_env: Callable[[], dict[str, str]],
        config_dir: Path,
        on_local_turn: Callable[[str, str, str], Awaitable[None]],
        audit,
    ):
        self.nodes = nodes
        self.get_settings = get_settings
        self.emit = emit
        self.askpass_env = askpass_env
        self.config_dir = config_dir
        self.on_local_turn = on_local_turn
        self.audit = audit

    def github_dir(self) -> str:
        return str(self.get_settings()["paths"].get("githubDir") or "")

    def mcp_config_path(self) -> str:
        port = int(self.get_settings()["node"].get("port", 8790))
        cfg_file = self.config_dir / "mcp_bridge_generated.json"
        payload = {"mcpServers": {"llm-orchestration": {
            "command": shutil.which("python3") or "/usr/bin/python3",
            "args": [str(ROOT_DIR / "server" / "mcp_bridge.py")],
            "env": {"LLM_ORCH_URL": f"http://127.0.0.1:{port}"},
        }}}
        cfg_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(cfg_file)

    async def run_local(self, agent: str, prompt: str, *, session_id: str = "", cwd: str = "", model: str = "",
                        effort: str = "", mode: str = "", fallback_model: str = "", chat_id: str = "", source: str = "orchestrator",
                        system_prompt: str = "") -> dict[str, Any]:
        kind = AGENT_IDS.get(agent)
        if not kind:
            raise AgentUnavailable(f"Unknown agent '{agent}'")
        s = self.get_settings()
        engines = s.get("engines") or {}
        timeout = float(s["terminal"].get("agentTimeoutSec", 3600))
        workdir = cwd.strip() or self.github_dir() or str(Path.home())
        if not Path(workdir).expanduser().is_dir():
            raise AgentUnavailable(f"Working directory does not exist on {self.nodes.local_id}: {workdir}")
        await self.on_local_turn(workdir, kind, source)
        self.audit.record("agent_turn", workdir, agent=kind, model=model, chatId=chat_id, prompt=prompt[:300])
        env = self.askpass_env()
        if kind == "agy-cli":
            ac = engines.get("agy") or {}
            if system_prompt and not session_id:
                prompt = f"{system_prompt}\n\n---\n\n{prompt}"
            coro = run_agy_turn(
                prompt, conversation_id=session_id, cwd=workdir,
                model=model if model not in ("", "agy", "agy-cli") else str(ac.get("model") or ""),
                effort=effort or str(ac.get("effort") or ""), mode=mode or str(ac.get("mode") or ""),
                dangerously_skip_permissions=bool(ac.get("dangerouslySkipPermissions", False)),
                emit_event=self.emit, chat_id=chat_id, node_id=self.nodes.local_id, extra_env=env,
            )
        else:
            cc = engines.get("claude") or {}
            coro = run_claude_turn(
                prompt, session_id=session_id, cwd=workdir,
                model=model if model not in ("", "claude", "claude-cli") else str(cc.get("model") or ""),
                fallback_model=fallback_model or str(cc.get("fallbackModel") or ""),
                effort=effort or str(cc.get("effort") or ""), mcp_config=self.mcp_config_path(),
                dangerously_skip_permissions=bool(cc.get("dangerouslySkipPermissions", False)),
                emit_event=self.emit, chat_id=chat_id, node_id=self.nodes.local_id, extra_env=env, system_prompt=system_prompt,
            )
        try:
            res = await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise AgentUnavailable(f"{kind} turn exceeded {int(timeout)}s and was stopped")
        res["executedNode"] = self.nodes.local_id
        res["cwd"] = workdir
        return res

    async def dispatch(self, target: str, agent: str, prompt: str, *, on_offline: str = "fail", cwd: str = "", **kw: Any) -> dict[str, Any]:
        """Run on `target`. If it's offline: on_offline='fail' raises, 'local' runs here with a visible note."""
        if self.nodes.is_local(target):
            return await self.run_local(agent, prompt, cwd=cwd, **kw)
        remote_cwd = await self.nodes.path_for_peer(target, cwd)
        try:
            r = await self.nodes.request(
                target, "POST", "/api/internal/agent_turn",
                json={"agent": agent, "prompt": prompt, "cwd": remote_cwd, "sessionId": kw.get("session_id", ""),
                      "model": kw.get("model", ""), "effort": kw.get("effort", ""), "mode": kw.get("mode", ""),
                      "fallbackModel": kw.get("fallback_model", ""), "chatId": kw.get("chat_id", ""),
                      "source": kw.get("source", "peer"), "systemPrompt": kw.get("system_prompt", "")},
                timeout=float(self.get_settings()["terminal"].get("agentTimeoutSec", 3600)) + 30,
            )
            if r.status_code >= 400:
                raise AgentUnavailable(f"{target} refused the turn: HTTP {r.status_code} {r.text[:300]}")
            out = r.json()
            out["executedNode"] = target
            return out
        except PeerOffline:
            if on_offline != "local":
                raise AgentUnavailable(f"Node '{target}' is offline; the turn was not run anywhere.")
            kw.pop("session_id", None)  # the peer's session doesn't exist here
            res = await self.run_local(agent, prompt, cwd=cwd, **kw)
            res["fallbackFrom"] = target
            res["fallbackReason"] = f"{target} offline"
            return res
