"""Live CLI subprocess runners for AGY (`agy`) and Claude Code (`claude`) with stream-json parsing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Awaitable, Callable

DEFAULT_AGY_MODELS: list[dict[str, str]] = [
    {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"},
    {"id": "gemini-4-neon-xhigh", "label": "Gemini 4 Neon (XHigh)"},
    {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)"},
    {"id": "gemini-4-neon-low", "label": "Gemini 4 Neon (Low)"},
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)"},
    {"id": "gemini-4-hydrogen-high", "label": "Gemini 4 Hydrogen (High)"},
    {"id": "gemini-4-hydrogen-xhigh", "label": "Gemini 4 Hydrogen (XHigh)"},
    {"id": "gemini-pro-orchestrator-neon", "label": "Orchestrator (Neon)"},
    {"id": "gemini-pro-orchestrator-g1-pro", "label": "Orchestrator (Argon)"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
    {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
    {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
]

CLAUDE_MODEL_PRESETS: list[dict[str, str]] = [
    {"id": "opus", "label": "Claude Opus (latest alias)"},
    {"id": "sonnet", "label": "Claude Sonnet (latest alias)"},
    {"id": "haiku", "label": "Claude Haiku (fast alias)"},
]


def resolve_cli_binary(name: str) -> str | None:
    local = Path.home() / ".local" / "bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def list_agy_models() -> list[dict[str, str]]:
    """Query `agy models` live when available, falling back cleanly if offline."""
    binary = resolve_cli_binary("agy")
    if not binary:
        return list(DEFAULT_AGY_MODELS)
    try:
        proc = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=6)
        if proc.returncode != 0:
            return list(DEFAULT_AGY_MODELS)
        parsed: list[dict[str, str]] = []
        for line in proc.stdout.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("Fetching"):
                continue
            parts = clean.split("\t", 1)
            mid = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else mid
            if mid:
                parsed.append({"id": mid, "label": label})
        return parsed or list(DEFAULT_AGY_MODELS)
    except Exception:
        return list(DEFAULT_AGY_MODELS)


AGY_EFFORT_TIERS = ("xhigh", "medium", "high", "low", "max")


def extract_agy_model_effort(model: str) -> str:
    """Return the built-in effort tier if an AGY model ID encodes one (e.g. `-medium`)."""
    low = (model or "").strip().lower()
    for tier in AGY_EFFORT_TIERS:
        if low.endswith(f"-{tier}"):
            return tier
    return ""


DEFAULT_STREAM_LIMIT = 16 * 1024 * 1024  # 16 MiB so large NDJSON tool results never trigger chunk-over-limit


async def _iter_ndjson_lines(stream: asyncio.StreamReader):
    """Read newline-delimited JSON safely even when individual lines exceed standard 64 KiB pipe buffers."""
    buf = bytearray()
    while True:
        chunk = await stream.read(262144)
        if not chunk:
            if buf:
                yield bytes(buf)
            break
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx == -1:
                break
            line = bytes(buf[:idx])
            del buf[: idx + 1]
            yield line


def build_agy_command(
    binary: str,
    prompt: str,
    conversation_id: str = "",
    model: str = "",
    effort: str = "",
    mode: str = "",
    dangerously_skip_permissions: bool = True,
) -> list[str]:
    cmd = [
        binary,
        "--output-format=stream-json",
        f"-p={prompt}",
    ]
    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if conversation_id:
        cmd.append(f"--conversation={conversation_id}")
    if model and model not in ("agy", "agy-cli"):
        cmd.append(f"--model={model}")
    model_effort = extract_agy_model_effort(model)
    if effort in AGY_EFFORT_TIERS and (not model_effort or effort == model_effort):
        cmd.append(f"--effort={effort}")
    if mode in ("accept-edits", "auto", "plan"):
        cmd.append(f"--mode={mode}")
    return cmd


def build_claude_command(
    binary: str,
    prompt: str,
    session_id: str = "",
    model: str = "",
    fallback_model: str = "",
    effort: str = "",
    mcp_config: str = "",
    dangerously_skip_permissions: bool = True,
    system_prompt: str = "",
) -> list[str]:
    # The prompt goes right after -p: --mcp-config takes several values and would swallow a
    # trailing prompt as a second config path.
    cmd = [
        binary,
        "-p",
        prompt,
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if session_id:
        cmd.extend(["--resume", session_id])
    if model and model not in ("claude", "claude-cli"):
        cmd.extend(["--model", model])
    if fallback_model:
        cmd.extend(["--fallback-model", fallback_model])
    if effort in ("low", "medium", "high", "xhigh", "max"):
        cmd.extend(["--effort", effort])
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    if mcp_config:
        cmd.extend(["--mcp-config", mcp_config])
    return cmd


def _agent_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra_env:
        for k, v in extra_env.items():
            if k == "PATH":
                env["PATH"] = f"{v}:{env.get('PATH', '')}"
            else:
                env[k] = v
    return env


async def run_agy_turn(
    prompt: str,
    conversation_id: str = "",
    cwd: str = "",
    model: str = "",
    effort: str = "",
    mode: str = "",
    dangerously_skip_permissions: bool = True,
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    chat_id: str = "",
    node_id: str = "local",
    extra_env: dict[str, str] | None = None,
    binary_override: str | None = None,
) -> dict[str, Any]:
    binary = binary_override or resolve_cli_binary("agy")
    if not binary:
        raise RuntimeError("`agy` CLI not found in ~/.local/bin or PATH")

    cmd = build_agy_command(
        binary,
        prompt,
        conversation_id=conversation_id,
        model=model,
        effort=effort,
        mode=mode,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    workdir = cwd.strip() or str(Path.home())
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=DEFAULT_STREAM_LIMIT,
        env=_agent_env(extra_env),
    )
    try:

        conv_id = conversation_id
        chunks: list[str] = []
        final_response = ""
        raw_errors: list[str] = []

        assert proc.stdout is not None
        async for line_b in _iter_ndjson_lines(proc.stdout):
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                raw_errors.append(line)
                continue

            etype = ev.get("event")
            if etype == "init" and ev.get("conversation_id"):
                conv_id = str(ev["conversation_id"])
            elif etype == "step_update":
                su = ev.get("step_update") or {}
                if su.get("conversation_id"):
                    conv_id = str(su["conversation_id"])
                delta = su.get("text_delta")
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    if emit_event:
                        await emit_event({"type": "agent_delta", "chatId": chat_id, "nodeId": node_id, "agent": "agy", "text": delta})
            elif etype == "result":
                res = ev.get("result") or {}
                if res.get("conversation_id"):
                    conv_id = str(res["conversation_id"])
                if res.get("response"):
                    final_response = str(res["response"])
                if res.get("error"):
                    raw_errors.append(str(res["error"]))

        stderr_b = await proc.stderr.read() if proc.stderr else b""
        rc = await proc.wait()
        text = (final_response or "".join(chunks)).strip()
        if not text and stderr_b:
            text = stderr_b.decode("utf-8", errors="replace").strip()
        if rc != 0 and raw_errors and not text:
            text = "\n".join(raw_errors)

        err_text = "\n".join(raw_errors + ([stderr_b.decode("utf-8", errors="replace").strip()] if stderr_b else [])).strip()
        return {
            "sessionId": conv_id,
            "response": text,
            "events": [],
            "exitCode": rc,
            "model": model or "agy-cli",
            "isError": rc != 0 or not (final_response or chunks),
            "errorText": err_text[:2000],
        }
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


async def run_claude_turn(
    prompt: str,
    session_id: str = "",
    cwd: str = "",
    model: str = "",
    fallback_model: str = "",
    effort: str = "",
    mcp_config: str = "",
    dangerously_skip_permissions: bool = True,
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    chat_id: str = "",
    node_id: str = "local",
    extra_env: dict[str, str] | None = None,
    binary_override: str | None = None,
    system_prompt: str = "",
) -> dict[str, Any]:
    binary = binary_override or resolve_cli_binary("claude")
    if not binary:
        raise RuntimeError("`claude` CLI not found in ~/.local/bin or PATH")

    cmd = build_claude_command(
        binary,
        prompt,
        session_id=session_id,
        model=model,
        fallback_model=fallback_model,
        effort=effort,
        mcp_config=mcp_config,
        dangerously_skip_permissions=dangerously_skip_permissions,
        system_prompt=system_prompt,
    )
    workdir = cwd.strip() or str(Path.home())
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=DEFAULT_STREAM_LIMIT,
        env=_agent_env(extra_env),
    )
    try:

        sess_id = session_id
        result_error = ""
        actual_model = model or "claude-cli"
        text_parts: list[str] = []
        events: list[dict[str, Any]] = []
        final_result = ""

        assert proc.stdout is not None
        async for line_b in _iter_ndjson_lines(proc.stdout):
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            if ev.get("session_id"):
                sess_id = str(ev["session_id"])

            etype = ev.get("type")
            if etype == "system" and ev.get("model"):
                actual_model = str(ev["model"])
            elif etype == "assistant":
                msg = ev.get("message") or {}
                if msg.get("model") and msg.get("model") != "<synthetic>":
                    actual_model = str(msg["model"])
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text":
                        t = str(blk.get("text") or "")
                        if t:
                            text_parts.append(t)
                            if emit_event:
                                await emit_event({"type": "agent_delta", "chatId": chat_id, "nodeId": node_id, "agent": "claude", "text": t})
                    elif btype == "thinking":
                        events.append({"type": "thinking", "text": str(blk.get("thinking") or "")})
                    elif btype == "tool_use":
                        tname = str(blk.get("name") or "tool")
                        tinput = blk.get("input") or {}
                        if tname in ("Edit", "Write") and isinstance(tinput, dict):
                            fpath = str(tinput.get("file_path") or "")
                            old_s = str(tinput.get("old_string") or "")
                            new_s = str(tinput.get("new_string") if tname == "Edit" else tinput.get("content") or "")
                            events.append({"type": "diff", "tool": tname, "path": fpath, "oldText": old_s, "newText": new_s})
                        else:
                            summary = str(tinput.get("command") or tinput.get("file_path") or tname) if isinstance(tinput, dict) else tname
                            events.append({"type": "tool", "tool": tname, "summary": summary, "detail": json.dumps(tinput, indent=2)[:1500]})
            elif etype == "result":
                if ev.get("result"):
                    final_result = str(ev["result"])
                if ev.get("is_error"):
                    result_error = str(ev.get("result") or ev.get("subtype") or "error")

        stderr_b = await proc.stderr.read() if proc.stderr else b""
        rc = await proc.wait()
        response_text = ("\n".join(text_parts).strip() or final_result).strip()
        if not response_text and stderr_b:
            response_text = stderr_b.decode("utf-8", errors="replace").strip()

        stderr_text = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        return {
            "sessionId": sess_id,
            "response": response_text,
            "events": events,
            "exitCode": rc,
            "model": actual_model,
            "isError": bool(result_error) or rc != 0,
            "errorText": (result_error + "\n" + stderr_text).strip()[:2000],
        }
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
