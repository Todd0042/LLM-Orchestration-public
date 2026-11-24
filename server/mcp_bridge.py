#!/usr/bin/env python3
"""Stdio MCP server exposing LLM-Orchestration peer relay, repo inventory, tools, and ~/LLM-Vault to AGY & Claude."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _resolve_hub_and_token() -> tuple[str, str]:
    cfg_dir = Path(os.environ.get("LLM_ORCH_CONFIG_DIR", str(Path.home() / ".config" / "llm-orchestration")))
    tok_file = cfg_dir / "node_token"
    tok = tok_file.read_text(encoding="utf-8").strip() if tok_file.is_file() else ""
    if os.environ.get("LLM_ORCH_URL"):
        return os.environ["LLM_ORCH_URL"].rstrip("/"), tok
    port = 8790
    settings_file = cfg_dir / "settings.json"
    if settings_file.is_file():
        try:
            port = int((json.loads(settings_file.read_text(encoding="utf-8")).get("node") or {}).get("port") or 8790)
        except Exception:
            pass
    # Always the local daemon: it knows this machine's repos and forwards to the other node itself.
    return f"http://127.0.0.1:{port}", tok


def _api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 180) -> Any:
    base_url, tok = _resolve_hub_and_token()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {"Content-Type": "application/json"} if data is not None else {}
    if tok:
        headers["X-Node-Token"] = tok
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


TOOLS: list[dict[str, Any]] = [
    {
        "name": "orch_peer_ask",
        "description": "Ask AGY or Claude on the Laptop Hub or Desktop Worker to run a task and reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "targetNode": {"type": "string", "description": "laptop, desktop, or auto"},
                "agent": {"type": "string", "description": "agy-cli or claude-cli"},
                "prompt": {"type": "string", "description": "Prompt for the target agent"},
                "cwd": {"type": "string", "description": "Optional working directory"},
            },
            "required": ["agent", "prompt"],
        },
    },
    {
        "name": "orch_repo_inventory",
        "description": "Inspect GitHub repo branches, dirty counts, and Laptop vs Desktop drift status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Rescan repositories now before returning"},
            },
        },
    },
    {
        "name": "orch_tool_inventory",
        "description": "List available AI models, CLIs, ComfyUI checkpoints, and Python tools per node.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "orch_vault_list",
        "description": "List files stored in ~/LLM-Vault/ shared hub storage.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "orch_vault_read",
        "description": "Read a text/markdown file from ~/LLM-Vault/.",
        "inputSchema": {
            "type": "object",
            "properties": {"relPath": {"type": "string"}},
            "required": ["relPath"],
        },
    },
    {
        "name": "orch_vault_write",
        "description": "Write or update a file inside ~/LLM-Vault/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "relPath": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["relPath", "content"],
        },
    },
    {
        "name": "orch_repo_preflight",
        "description": "Call this before starting work in a repo. Checks whether the other machine has newer or uncommitted work in it and brings that work here first (non-destructive; conflicts are reported, never merged). Returns a one-line status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Absolute path inside the repo (usually the current working directory)"},
                "repo": {"type": "string", "description": "Repo folder name, if cwd is not given"},
            },
        },
    },
    {
        "name": "orch_repo_board",
        "description": "Show every repo's state on each machine (branch, commit, uncommitted files), which machine is the active device, and recent handoffs.",
        "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean"}}},
    },
    {
        "name": "orch_intermission_queue",
        "description": "View queued Desktop intermission sync changes recorded while Desktop was offline.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


STATIC_NAMES = {t["name"] for t in TOOLS}


def _all_tools() -> list[dict[str, Any]]:
    """Bridge tools plus the orchestrator's own tools (same code the orchestrator uses)."""
    tools = list(TOOLS)
    try:
        for t in _api("GET", "/api/internal/orch-tools", timeout=15).get("tools", []):
            if t["name"] not in STATIC_NAMES:
                tools.append({"name": t["name"], "description": t.get("description", ""),
                              "inputSchema": t.get("parameters") or {"type": "object", "properties": {}}})
    except Exception:
        pass  # daemon unreachable: the bridge's own tools still work
    return tools


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    if name not in STATIC_NAMES and name.startswith("orch_"):
        # may wait for your Approve/Deny click or for an agent to finish
        return _api("POST", "/api/internal/orch-tool", {"name": name, "arguments": args, "chatId": os.environ.get("LLM_ORCH_CHAT_ID", "")}, timeout=3600).get("result")
    if name == "orch_peer_ask":
        return _api(
            "POST",
            "/api/mcp/peer-ask",
            {
                "targetNode": args.get("targetNode") or "auto",
                "agent": args.get("agent") or "claude-cli",
                "prompt": args.get("prompt") or "",
                "cwd": args.get("cwd") or "",
            },
        )
    if name == "orch_repo_inventory":
        refresh = "true" if args.get("refresh") else "false"
        return _api("GET", f"/api/repos/inventory?refresh={refresh}")
    if name == "orch_tool_inventory":
        return _api("GET", "/api/tools/inventory")
    if name == "orch_vault_list":
        return _api("GET", "/api/vault/list")
    if name == "orch_vault_read":
        q = urllib.parse.quote(str(args.get("relPath") or ""))
        return _api("GET", f"/api/vault/read?relPath={q}")
    if name == "orch_vault_write":
        return _api(
            "POST",
            "/api/vault/write",
            {"relPath": str(args.get("relPath") or ""), "content": str(args.get("content") or "")},
        )
    if name == "orch_repo_preflight":
        return _api("POST", "/api/repos/preflight", {"cwd": str(args.get("cwd") or ""), "repo": str(args.get("repo") or ""), "source": "mcp"})
    if name == "orch_repo_board":
        return _api("GET", f"/api/repos/board?refresh={'true' if args.get('refresh') else 'false'}")
    if name == "orch_intermission_queue":
        return _api("GET", "/api/sync/queue")
    raise ValueError(f"Unknown tool: {name}")


def _write_msg(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        clean = line.strip()
        if not clean:
            continue
        try:
            req = json.loads(clean)
        except Exception:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "initialize":
            _write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "llm-orchestration-bridge", "version": "2.0.0"},
                        "capabilities": {"tools": {}},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write_msg({"jsonrpc": "2.0", "id": rid, "result": {"tools": _all_tools()}})
        elif method == "tools/call":
            tname = params.get("name", "")
            targs = params.get("arguments") or {}
            try:
                out = _call_tool(tname, targs)
                _write_msg(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}]},
                    }
                )
            except Exception as exc:
                _write_msg(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {exc}"}],
                            "isError": True,
                        },
                    }
                )
        elif rid is not None:
            _write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
