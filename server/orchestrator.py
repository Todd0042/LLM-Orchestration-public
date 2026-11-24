"""The Orchestrator: one conversation that routes work to local LLMs, AGY, Claude, and tools.

A turn runs as a background job (see core.JobRunner) and streams progress over /ws/events;
the HTTP request that starts it returns immediately (Cloudflare cuts requests at ~100 s).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import time
from typing import Any
import uuid

from .agents import AgentUnavailable
from .core import is_read_only_command
from .gpu_and_media import generate_comfy_media
from .agent_chain import AGENT_NAMES, is_usage_limit, until_text
from .llm import CLI_MODEL_IDS, LlmError
from .repo_ops import find_repo
from .orch_tools import build_orchestrator_system_prompt, builtin_tool_schemas, execute_builtin_tool
from .tools_loader import load_python_tools

MAX_STEPS = 6
TASK_FOR_KIND = {"code": "coding", "reasoning": "reasoning", "research": "research"}


def compact_messages(msgs: list[dict[str, Any]], max_chars: int = 12500) -> list[dict[str, Any]]:
    """Keep the system prompt + active turn; drop the oldest history (never orphaning tool results)."""
    if not msgs:
        return msgs
    sys_msg = msgs[0] if msgs[0].get("role") == "system" else None
    rest = msgs[1:] if sys_msg is not None else list(msgs)
    last_user = max((i for i, m in enumerate(rest) if m.get("role") == "user"), default=0)
    earlier, active = rest[:last_user], [dict(m) for m in rest[last_user:]]

    def size(m: dict[str, Any]) -> int:
        return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or ""))

    sys_len = size(sys_msg) if sys_msg else 0
    active_len = sum(size(m) for m in active) + sys_len
    if active_len > max_chars:
        tool_idx = [i for i, m in enumerate(active) if m.get("role") == "tool"]
        if tool_idx:
            per_tool = max(1200, max(1500, max_chars - sys_len - 800) // len(tool_idx))
            for i in tool_idx:
                c = str(active[i].get("content") or "")
                if len(c) > per_tool:
                    active[i]["content"] = c[:per_tool] + " ...[truncated for context window]"
        active_len = sum(size(m) for m in active) + sys_len

    groups: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(earlier):
        m = earlier[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            grp = [m]
            i += 1
            while i < len(earlier) and earlier[i].get("role") == "tool":
                grp.append(earlier[i])
                i += 1
            groups.append(grp)
        else:
            if m.get("role") != "tool":
                groups.append([m])
            i += 1
    kept: list[dict[str, Any]] = []
    used = active_len
    for grp in reversed(groups):
        g = sum(size(x) for x in grp)
        if used + g > max_chars:
            break
        used += g
        kept = grp + kept
    return ([sys_msg] if sys_msg else []) + kept + active


class Orchestrator:
    def __init__(self, ctx):
        self.ctx = ctx

    # ---- persistence helpers --------------------------------------------------------------------
    def add_message(self, chat_id: str, role: str, label: str, node: str, model: str, content: str, events: list | None = None) -> str:
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        self.ctx.conn.execute(
            "INSERT INTO messages (id, chat_id, role, sender_label, node_id, model_id, content, events_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, chat_id, role, label, node, model, content, json.dumps(events or []), time.time()),
        )
        self.ctx.conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (time.time(), chat_id))
        self.ctx.conn.commit()
        return mid

    async def progress(self, chat_id: str, step: str, detail: str = "", done: bool = False) -> None:
        await self.ctx.emit({"type": "orch_progress", "chatId": chat_id, "step": step, "detail": detail, "done": done})

    # ---- entry point ------------------------------------------------------------------------------
    def start_turn(self, chat_id: str, body: dict[str, Any], zone: str) -> str:
        row = self.ctx.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            raise KeyError(chat_id)
        self.add_message(chat_id, "user", "You", self.ctx.nodes.local_id, "", str(body.get("content") or ""))
        self.ctx.conn.execute("UPDATE chats SET task_type=? WHERE id=?", (str(body.get("taskType") or "coding"), chat_id))
        self.ctx.conn.commit()

        async def work(job_id: str) -> dict[str, Any]:
            await self.ctx.emit({"type": "chat_updated", "chatId": chat_id})
            try:
                return await self._turn(job_id, chat_id, body, zone)
            except Exception as exc:
                detail = str(exc)
                self.add_message(chat_id, "assistant", "Orchestrator Error", self.ctx.nodes.local_id, str(body.get("modelId") or ""),
                                 f"⚠️ **Orchestrator error:**\n\n`{detail[:600]}`")
                raise
            finally:
                await self.ctx.emit({"type": "chat_updated", "chatId": chat_id})
                await self.progress(chat_id, "Done", done=True)

        return self.ctx.jobs.start("orchestrator_turn", f"Turn in {chat_id}", work,
                                   timeout_sec=float(self.ctx.settings()["terminal"].get("agentTimeoutSec", 3600)) + 300,
                                   payload={"chatId": chat_id})

    async def _turn(self, job_id: str, chat_id: str, body: dict[str, Any], zone: str) -> dict[str, Any]:
        ctx = self.ctx
        s = ctx.settings()
        row = ctx.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        task = str(body.get("taskType") or "coding")
        route = await ctx.llm.resolve(str(body.get("nodeId") or "auto"), task, str(body.get("modelId") or ""))
        await self.progress(chat_id, f"Routing to {route.model}", f"Node: @{route.node}" + (f" · {route.reason}" if route.reason else ""))
        tdef = s["taskDefaults"].get(task) or s["taskDefaults"]["coding"]
        content = str(body.get("content") or "")

        if task in ("image", "video"):
            ckpt = str(body.get("modelId") or tdef["modelId"])
            res = await generate_comfy_media(ctx.broker, s, task, content, ckpt, chat_id)
            ctx.conn.execute(
                "INSERT INTO artifacts (id, chat_id, kind, title, path, node_id, mime_type, meta_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"art_{uuid.uuid4().hex[:10]}", chat_id, res["kind"], res["filename"], res["path"], ctx.nodes.local_id,
                 "image/png" if res["kind"] == "image" else "video/mp4", json.dumps(res), time.time()),
            )
            self.add_message(chat_id, "assistant", "ComfyUI", ctx.nodes.local_id, ckpt,
                             f"Generated {res['kind']} with `{ckpt}` (seed `{res['seed']}`):\n\n[{res['filename']}](file://{res['path']})",
                             [{"type": "media", "kind": res["kind"], "path": res["path"], "filename": res["filename"], "nodeId": ctx.nodes.local_id}])
            return {"route": route.__dict__}

        if route.model in CLI_MODEL_IDS:  # you picked an agent yourself: start the chain with it
            return await self._chain_turn(job_id, chat_id, row, body, zone, content, "coding",
                                          self.chain_for("coding", start=route.model), self.detect_repo(content))

        kind, repo = await self.classify(route, content)
        await self.progress(chat_id, f"Looks like {kind}" + (f" about {repo}" if repo else ""), f"router: {route.model}")
        if kind in TASK_FOR_KIND:
            task_key = TASK_FOR_KIND[kind]
            return await self._chain_turn(job_id, chat_id, row, body, zone, content, task_key, self.chain_for(task_key), repo)
        if kind == "story":
            sdef = s["taskDefaults"]["story"]
            sroute = await ctx.llm.resolve(str(sdef.get("nodeId") or "auto"), "story", str(sdef.get("modelId") or route.model))
            return await self._llm_loop(job_id, chat_id, row, {**body, "runTools": False}, sroute, sdef, zone)
        return await self._llm_loop(job_id, chat_id, row, body, route, tdef, zone)

    # ---- routing: a local model sorts the request; heavy kinds go to the agent chain ---------------
    def repo_names(self) -> list[str]:
        rows = self.ctx.conn.execute("SELECT DISTINCT repo_name FROM repo_inventory").fetchall()
        return sorted({r["repo_name"] for r in rows}, key=len, reverse=True)

    def detect_repo(self, text: str) -> str:
        low = text.lower()
        for name in self.repo_names():
            if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", low):
                return name
        return ""

    async def classify(self, route, content: str) -> tuple[str, str]:
        """Sort a request into code / reasoning / research / story / ops / chat. Cheap local call."""
        detected = self.detect_repo(content)
        system = (
            "You route requests for a personal software assistant. Reply with ONLY a JSON object like "
            '{"kind": "code", "repo": ""}. kind is one of: '
            "code (reading, writing, fixing, reviewing or explaining code, or anything about what is inside a repo), "
            "reasoning (hard analysis, planning, maths, decisions), research (needs current information from the web or docs), "
            "story (creative writing), ops (the user's machines, repo syncing, sites, vault, running commands, status), "
            "chat (small talk or a quick question answerable reliably without looking anything up). "
            "repo is the repository the request is about, or empty."
        )
        user = f"Repos: {', '.join(self.repo_names()[:150])}\n\nRequest:\n{content[:3000]}"
        kind, repo = "", ""
        try:
            r = await self.ctx.llm.complete(route, [{"role": "system", "content": system}, {"role": "user", "content": user}], 0.0, 800)
            text = str(((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            kind = str(data.get("kind") or "").strip().lower()
            repo = str(data.get("repo") or "").strip()
        except (LlmError, ValueError, AttributeError):
            pass
        if kind not in ("code", "reasoning", "research", "story", "ops", "chat"):
            kind = "code" if detected else "chat"
        if repo and repo.lower() not in {n.lower() for n in self.repo_names()}:
            repo = ""
        return kind, (detected or repo)

    def chain_for(self, task: str, start: str = "") -> list[str]:
        """Claude -> AGY -> local model, as configured per task (`chain`), optionally starting elsewhere."""
        s = self.ctx.settings()
        tdef = s["taskDefaults"].get(task) or s["taskDefaults"]["coding"]
        chain = [str(x) for x in (tdef.get("chain") or []) if x] or ["claude-cli", "agy-cli", str(tdef.get("modelId") or "")]
        if start:
            chain = [start] + [x for x in chain if x != start]
        if not any(x not in CLI_MODEL_IDS for x in chain):  # always end on something free
            chain.append(str(s["engines"]["orchestrator"].get("modelId") or "qwen3-8b"))
        return [x for x in chain if x]

    def _handoff_prompt(self, chat_id: str, content: str, previous: str) -> str:
        rows = self.ctx.conn.execute(
            "SELECT sender_label, content FROM messages WHERE chat_id=? ORDER BY created_at DESC LIMIT 9", (chat_id,)).fetchall()[1:]
        history = "\n\n".join(f"{r['sender_label']}: {str(r['content'])[:1200]}" for r in reversed(rows) if r["content"])
        if not history and not previous:
            return content
        parts = []
        if history:
            parts.append(f"Conversation so far:\n{history}")
        if previous:
            parts.append(f"{previous} was handling this and couldn't continue (out of usage). Pick up from here.")
        parts.append(f"Current request:\n{content}")
        return "\n\n---\n\n".join(parts)

    async def run_agents(self, chat_id: str, task: str, content: str, chain: list[str], repo: str, *,
                         engine_model: str = "", effort: str = "", force_first: bool = False) -> dict[str, Any]:
        """Try each paid agent in order. Returns {ok, res, agent, notes} or {ok: False, notes, rest}."""
        ctx = self.ctx
        s = ctx.settings()
        row = ctx.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone() if chat_id else None
        pconf = json.loads(row["peer_config_json"] or "{}") if row else {}
        found = find_repo(ctx.github_dir(), repo) if repo else None
        raw_ws = str(row["workspace"] or "") if row else ""
        if found:
            cwd = str(found)
        elif raw_ws and Path(raw_ws).expanduser().is_dir():
            cwd = str(Path(raw_ws).expanduser())
        else:
            # Peer workspaces (e.g. the other machine's repos folder) fall back cleanly to local github_dir
            cwd = str(ctx.github_dir())
        notes: list[str] = []
        previous = ""
        for i, agent in enumerate(chain):
            if agent not in CLI_MODEL_IDS:
                return {"ok": False, "notes": notes, "rest": chain[i:], "previous": previous}
            name = AGENT_NAMES.get(agent, agent)
            if not ctx.availability.available(agent) and not (force_first and i == 0):  # you picked it yourself: try anyway
                until = ctx.availability.snapshot().get(agent, {}).get("until", 0)
                notes.append(f"{name} out of usage until {until_text(until)}" if until else f"{name} unavailable")
                previous = previous or ""
                continue
            key = f"{agent}_{ctx.nodes.local_id}_session"
            resume = str(pconf.get(key) or "")
            prompt = content if (resume and not previous) else self._handoff_prompt(chat_id, content, previous) if chat_id else content
            cfg = (s.get("engines") or {}).get("claude" if agent == "claude-cli" else "agy") or {}
            await self.progress(chat_id, f"{task}: {name}" + (f" in {repo}" if repo else ""), " · ".join(notes) or "working")
            try:
                res = await ctx.agents.dispatch("local", agent, prompt, on_offline="local", system_prompt=await self.briefing(),
                                                session_id=resume, cwd=cwd, model=(engine_model if i == 0 and engine_model else str(cfg.get("model") or "")),
                                                effort=effort or str(cfg.get("effort") or ""), chat_id=chat_id)
            except (AgentUnavailable, RuntimeError, OSError) as exc:  # e.g. CLI not installed on this machine
                notes.append(f"{name} unavailable ({str(exc)[:80]})")
                previous = name
                continue
            text = str(res.get("response") or "")
            err = str(res.get("errorText") or "")
            if res.get("isError") or not text.strip():
                if is_usage_limit(err) or is_usage_limit(text):
                    until = ctx.availability.mark_exhausted(agent, f"{err}\n{text}")
                    notes.append(f"{name} out of usage until {until_text(until)}")
                    ctx.audit.record("agent_out_of_usage", agent, until=until)
                    await ctx.emit({"type": "agent_availability", "agent": agent, "until": until})
                else:
                    notes.append(f"{name} failed: {(err or text or 'no answer')[:100]}")
                previous = name
                continue
            ctx.availability.clear(agent)  # it worked, so it isn't out (any more)
            if res.get("sessionId") and row:
                pconf[key] = res["sessionId"]
                ctx.conn.execute("UPDATE chats SET peer_config_json=? WHERE id=?", (json.dumps(pconf), chat_id))
                ctx.conn.commit()
            return {"ok": True, "res": res, "agent": agent, "notes": notes}
        return {"ok": False, "notes": notes, "rest": [], "previous": previous}

    async def _chain_turn(self, job_id, chat_id, row, body, zone, content, task, chain, repo) -> dict[str, Any]:
        ctx = self.ctx
        s = ctx.settings()
        out = await self.run_agents(chat_id, task, content, chain, repo,
                                    engine_model=str(body.get("engineModel") or "") if str(body.get("modelId") or "") == chain[0] else "",
                                    effort=str(body.get("effort") or ""), force_first=str(body.get("modelId") or "") in CLI_MODEL_IDS)
        notes = out["notes"]
        if out["ok"]:
            res, agent = out["res"], out["agent"]
            exec_node = str(res.get("executedNode") or ctx.nodes.local_id)
            label = f"{AGENT_NAMES.get(agent, agent)} ({exec_node}) · {task}" + (f" · {'; '.join(notes)}" if notes else "")
            self.add_message(chat_id, "assistant", label, exec_node, str(res.get("model") or agent), str(res.get("response") or ""), res.get("events") or [])
            return {"agent": agent, "notes": notes}
        local = next((m for m in out["rest"] if m not in CLI_MODEL_IDS), "")
        if not local:
            self.add_message(chat_id, "assistant", "Orchestrator", ctx.nodes.local_id, "", "⚠️ No agent could take this: " + "; ".join(notes))
            return {"agent": None, "notes": notes}
        tdef = s["taskDefaults"].get(task) or s["taskDefaults"]["coding"]
        route = await ctx.llm.resolve("auto", task, local)
        route.fallback = True
        route.reason = "; ".join(notes + [f"answered by local {route.model}"])
        prompt = self._handoff_prompt(chat_id, content, out.get("previous") or "") if out.get("previous") else content
        return await self._llm_loop(job_id, chat_id, row, {**body, "content": prompt}, route, tdef, zone)

    async def _llm_loop(self, job_id, chat_id, row, body, route, tdef, zone) -> dict[str, Any]:
        ctx = self.ctx
        s = ctx.settings()
        local_cfg = (s.get("engines") or {}).get("local") or {}
        history = ctx.conn.execute("SELECT role, content FROM messages WHERE chat_id=? ORDER BY created_at ASC", (chat_id,)).fetchall()
        tools_reg = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        peers_online = {pid: (await ctx.nodes.check(pid))[0] for pid in ctx.nodes.peer_ids()}
        base_prompt = str(local_cfg.get("systemPrompt") or "").strip() or str(tdef["systemPrompt"])
        sys_prompt = build_orchestrator_system_prompt(base_prompt, ctx.conn, s, any(peers_online.values()), tools_reg)
        msgs: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}] + [
            {"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")
        ]
        tools = (builtin_tool_schemas() + [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools_reg.values() if t["enabled"]
        ]) if body.get("runTools", True) else None
        temperature = float(tdef.get("temperature", local_cfg.get("temperature", 0.2)))
        max_tokens = max(int(tdef.get("maxTokens", local_cfg.get("maxTokens", 4096))), 4096)
        events: list[dict[str, Any]] = []
        choice: dict[str, Any] = {}

        async def on_wait(text: str) -> None:
            await self.progress(chat_id, text, "gpu-scheduler queue")

        for step in range(MAX_STEPS):
            await self.progress(chat_id, f"Step {step + 1}: thinking with {route.model} (@{route.node})…", "Waiting for response or tool call")
            try:
                resp = await ctx.llm.complete(route, compact_messages(msgs, 13500), temperature, max_tokens, tools, job=job_id, on_wait=on_wait)
            except LlmError as exc:
                if "context" not in str(exc).lower():
                    raise
                resp = await ctx.llm.complete(route, compact_messages(msgs, 7500), temperature, max_tokens, tools, job=job_id, on_wait=on_wait)
            choice = (resp.get("choices") or [{}])[0].get("message") or {}
            reasoning = str(choice.get("reasoning_content") or choice.get("reasoning") or "").strip()
            if reasoning:
                events.append({"type": "thinking", "text": reasoning})
            calls = choice.get("tool_calls") or []
            raw = str(choice.get("content") or "").strip()
            if not calls and tools and raw.startswith("{") and raw.endswith("}"):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and isinstance(parsed.get("name"), str) and isinstance(parsed.get("arguments"), dict):
                        calls = [{"id": f"fallback_{uuid.uuid4().hex[:8]}", "type": "function",
                                  "function": {"name": parsed["name"], "arguments": json.dumps(parsed["arguments"])}}]
                        choice = {"role": "assistant", "content": "", "tool_calls": calls}
                except ValueError:
                    pass
            if not calls:
                break
            msgs.append(choice)
            seen: dict[str, Any] = {}
            for tc in calls:
                fn = (tc.get("function") or {}).get("name", "")
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                sig = f"{fn}::{json.dumps(args, sort_keys=True)}"
                if sig not in seen:
                    await self.progress(chat_id, f"Running tool: {fn}", ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items()) or "executing…")
                    seen[sig] = await self._run_tool(fn, args, chat_id, row, tools_reg, peers_online, zone)
                    events.append({"type": "tool", "tool": fn, "summary": json.dumps(args), "detail": json.dumps(seen[sig], indent=2, default=str)[:2500]})
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", fn), "content": json.dumps(seen[sig], default=str)[:7000]})

        label = f"Agent ({route.node})" + (f" · {route.reason}" if route.fallback else "")
        self.add_message(chat_id, "assistant", label, route.node, route.model, str(choice.get("content") or "").strip(), events)
        return {"route": route.__dict__, "steps": len(events)}

    # ---- tools ------------------------------------------------------------------------------------
    async def call_tool(self, fn: str, args: dict[str, Any], *, chat_id: str = "", zone: str = "internal") -> Any:
        """Run one orchestrator tool for an agent orchestrator (Claude/AGY via the MCP bridge).
        Same implementation the local-model loop uses, including the approval card for commands."""
        s = self.ctx.settings()
        tools_reg = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        peers_online = {pid: (await self.ctx.nodes.check(pid))[0] for pid in self.ctx.nodes.peer_ids()}
        return await self._run_tool(fn, args, chat_id, None, tools_reg, peers_online, zone)

    async def briefing(self) -> str:
        """What an agent orchestrator needs to know: machines, routing table, how to reach the tools."""
        s = self.ctx.settings()
        tools_reg = await asyncio.to_thread(load_python_tools, Path(s["paths"]["toolsDir"]), s.get("disabledTools"))
        online = any([(await self.ctx.nodes.check(pid))[0] for pid in self.ctx.nodes.peer_ids()])
        base = build_orchestrator_system_prompt(
            "You are the user's orchestrator: the one assistant they always talk to, across their two machines.",
            self.ctx.conn, s, online, tools_reg)
        return base + "\n\n" + "\n".join([
            "HOW YOU WORK HERE:",
            f"- You run as an agent on `{self.ctx.nodes.local_id}` with your own file and shell tools. Repos live under {self.ctx.github_dir()}.",
            "- The orchestrator tools are available through the `llm-orchestration` MCP server (tool names start with `orch_`): use them for "
            "the other machine, repo sync and handoffs, the vault, sites, local models, and the user's task routing table.",
            "- Before working in a repo, call `orch_repo_preflight` with its path so the other machine's newer work is brought over first.",
            "- For story writing, call `orch_run_specialist_llm` with taskType 'story' (the user's local uncensored model). For code, do it yourself in the repo.",
            "- Never invent results: if a tool or file doesn't give you the answer, say so.",
        ])

    async def _run_tool(self, fn, args, chat_id, row, tools_reg, peers_online, zone) -> Any:
        ctx = self.ctx
        s = ctx.settings()
        try:
            if fn.startswith("orch_"):
                return await execute_builtin_tool(
                    fn, args, conn=ctx.conn, settings=s, node_id=ctx.nodes.local_id, github_dir=ctx.github_dir(),
                    vault_dir=ctx.vault_dir(), desktop_online=any(peers_online.values()), tools_reg=tools_reg,
                    dispatch_cli_agent=self._delegate, sync_repo_fn=self._sync_repo,
                    run_specialist_llm_fn=self._specialist, run_command_fn=lambda n, c, d: self._command(n, c, d, chat_id, zone),
                )
            tool = tools_reg.get(fn)
            if not tool or not tool["enabled"]:
                return {"error": f"Tool {fn} unavailable or disabled"}
            ctx.audit.record("python_tool", fn, zone=zone, args=args)
            return await asyncio.to_thread(tool["run"], **args)
        except (AgentUnavailable, LlmError, PermissionError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def _delegate(self, target_node: str = "auto", agent_kind: str = "agy-cli", prompt: str = "", cwd: str = "", model: str = "", **_: Any) -> dict[str, Any]:
        return await self.ctx.agents.dispatch(target_node, agent_kind, prompt, cwd=cwd, model=model, on_offline="fail" if target_node not in ("", "auto") else "local")

    async def _sync_repo(self, repo: str, action: str, target: str, agent: str) -> dict[str, Any]:
        return await self.ctx.repo_sync.sync_action(repo, action, target, agent)

    async def _specialist(self, task: str, model: str, node: str, prompt: str, repo: str = "") -> dict[str, Any]:
        """Hand a task to its configured chain (Claude -> AGY -> local) or to one explicit model."""
        s = self.ctx.settings()
        sp = s["taskDefaults"].get(task) or s["taskDefaults"]["coding"]
        chain = [model] if model else self.chain_for(task) if (sp.get("chain") or sp.get("modelId") in CLI_MODEL_IDS) else [str(sp.get("modelId") or "")]
        mid = chain[0]
        notes: list[str] = []
        if any(c in CLI_MODEL_IDS for c in chain):
            out = await self.run_agents("", task, prompt, chain, repo)
            notes = out["notes"]
            if out["ok"]:
                res = out["res"]
                return {"agent": out["agent"], "node": res.get("executedNode"), "notes": notes, "response": res.get("response")}
            mid = next((c for c in out["rest"] if c not in CLI_MODEL_IDS), "")
            if not mid:
                return {"error": "No agent could take this", "notes": notes}
        route = await self.ctx.llm.resolve(node or str(sp.get("nodeId") or "auto"), task, mid)
        r = await self.ctx.llm.complete(route, [{"role": "system", "content": str(sp.get("systemPrompt") or "Respond concisely.")},
                                               {"role": "user", "content": prompt}],
                                        float(sp.get("temperature", 0.25)), int(sp.get("maxTokens", 2048)))
        text = str(((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return {"node": route.node, "model": route.model, "fallbackUsed": route.fallback or bool(notes), "notes": notes, "reason": route.reason, "response": text}

    async def _command(self, node: str, cmd: str, cwd: str, chat_id: str, zone: str) -> dict[str, Any]:
        """Commands chosen by a model need your click unless they're on the read-only list."""
        ctx = self.ctx
        term = ctx.settings()["terminal"]
        target = node if node and not ctx.nodes.is_local(node) else ctx.nodes.local_id
        read_only = bool(term.get("trustedReadOnlyAutoRun", True)) and is_read_only_command(cmd, term.get("readOnlyAllowlist"))
        if term.get("askApprovalBeforeRun", True) and not read_only:
            await self.progress(chat_id, "Waiting for your approval", cmd[:120])
            ok = await ctx.approvals.request(kind="command", node_id=target, command=cmd, cwd=cwd, chat_id=chat_id,
                                             timeout_sec=float(term.get("approvalTimeoutSec", 300)))
            ctx.audit.record("approval", cmd, zone=zone, approved=ok, node=target)
            if not ok:
                return {"executedNode": target, "exitCode": None, "output": "", "error": "Denied (or not approved in time) by the user."}
        ctx.audit.record("model_command", cmd, zone=zone, node=target, cwd=cwd, readOnly=read_only)
        if target != ctx.nodes.local_id:
            try:
                data = await ctx.nodes.json(target, "POST", "/api/terminal/run", json={"command": cmd, "cwd": cwd or "", "nodeId": "local"}, timeout=300.0)
            except Exception as exc:
                return {"executedNode": target, "exitCode": 1, "output": "", "error": str(exc)}
            data["executedNode"] = target
            return data
        res = await ctx.term.run_command(cmd, cwd=cwd or str(ctx.github_dir()), shell=str(term.get("shell") or "/usr/bin/bash"), chat_id="")
        res["executedNode"] = ctx.nodes.local_id
        return res

    # ---- peer relay -------------------------------------------------------------------------------
    def start_relay(self, chat_id: str, body: dict[str, Any]) -> str:
        row = self.ctx.conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            raise KeyError(chat_id)

        async def work(job_id: str) -> dict[str, Any]:
            self.add_message(chat_id, "user", "Relay Topic", self.ctx.nodes.local_id, "", body["topic"])
            await self.ctx.emit({"type": "chat_updated", "chatId": chat_id})
            last = body["topic"]
            turns = 0
            for turn in range(int(body.get("turns", 6))):
                a = turn % 2 == 0
                role = body["roleA"] if a else body["roleB"]
                route = await self.ctx.llm.resolve(body["nodeA"] if a else body["nodeB"], "peer_relay", body["modelA"] if a else body["modelB"])
                sys_p = f"You are {role} running on node '{route.node}'. Respond to the peer agent constructively. Include '[DONE]' if finished."
                await self.ctx.jobs.progress(job_id, f"Turn {turn + 1}: {role} @ {route.node}")
                try:
                    if route.model in CLI_MODEL_IDS:
                        res = await self.ctx.agents.dispatch(route.node, route.model, f"{sys_p}\n\nPeer message:\n{last}",
                                                             on_offline="local", cwd=str(row["workspace"] or ""), chat_id=chat_id)
                        reply, evs = str(res.get("response") or "").strip(), res.get("events") or []
                    else:
                        r = await self.ctx.llm.complete(route, [{"role": "system", "content": sys_p}, {"role": "user", "content": last}], 0.35, 1200, job=job_id)
                        reply, evs = str(((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip(), []
                except (AgentUnavailable, LlmError) as exc:
                    self.add_message(chat_id, "assistant", f"Relay stopped ({route.node})", route.node, route.model, f"⚠️ {exc}")
                    await self.ctx.emit({"type": "chat_updated", "chatId": chat_id})
                    raise
                self.add_message(chat_id, "assistant", f"{role} ({route.node}{' fallback' if route.fallback else ''})", route.node, route.model, reply, evs)
                await self.ctx.emit({"type": "chat_updated", "chatId": chat_id})
                turns += 1
                last = reply
                if "[DONE]" in reply:
                    break
            return {"turns": turns}

        return self.ctx.jobs.start("relay", f"Relay in {chat_id}", work, payload={"chatId": chat_id})
