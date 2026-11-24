"""Built-in Orchestrator function tools (settings/routing awareness, capabilities, specialist delegation, repo sync, Vault)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Awaitable, Callable

from .cli_agents import list_agy_models, resolve_cli_binary
from .config import model_dirs
from .gpu_and_media import discover_node_models
from .inventory_sync import (
    compare_repo_inventories,
    list_intermission_queue,
    list_vault_files,
    read_repo_inventory,
    read_tool_inventory,
    resolve_vault_path,
    scan_local_repos,
    sync_local_tool_inventory,
)


def builtin_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-compatible tool definitions available directly to the Orchestrator LLM."""
    return [
        {
            "type": "function",
            "function": {
                "name": "orch_get_settings",
                "description": (
                    "Read the user's current Orchestrator & Specialist Task Routing settings "
                    "(which exact model/agent/node is configured for coding, reasoning, story writing, research, image, video, music, AGY, and Claude)."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_run_specialist_llm",
                "description": (
                    "Hand a task to whatever the user configured for that task type in Task Defaults: "
                    "an agent (Claude Code / AGY, which can read and edit files and run commands) or a local model. "
                    "For coding, always pass `repo` so the agent works in the right project."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "Prompt for the specialist LLM."},
                        "taskType": {"type": "string", "description": "Task key (coding, reasoning, story, research) to use its configured model & settings."},
                        "modelId": {"type": "string", "description": "Optional explicit GGUF/LLM model ID override."},
                        "targetNode": {"type": "string", "description": "auto, laptop, or desktop"},
                        "repo": {"type": "string", "description": "Repo folder name (or absolute path) the task is about; agents run inside it."},
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_list_capabilities",
                "description": (
                    "Inspect all available CLI agents (agy-cli, claude-cli), local GGUF LLMs on disk/router, "
                    "ComfyUI image/video models, Python tools, and node online/offline status WITHOUT waking or loading any models."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "refresh": {"type": "boolean", "description": "Rescan disk/DB before returning (still does not wake LLMs)."}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_get_repo_status",
                "description": "Compare GitHub repositories across Laptop, Desktop, and GitHub Remote (`gh repo list`), including out-of-sync branches, dirty files, remote-only repos, and catch-up queue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repoName": {"type": "string", "description": "Optional single repository folder name to filter."},
                        "refresh": {"type": "boolean", "description": "Run a fresh local git status scan across the repos folder (default false)."},
                        "includeRemote": {"type": "boolean", "description": "Query GitHub remote repositories via `gh repo list` (default true)."},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_run_command",
                "description": "Run a shell, `git`, or `gh` CLI command on Laptop or Desktop (non-interactive git/gh status, branches, logs, fetches, tests, file checks).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run (e.g. `git -C ~/GitHub/app branch -a -vv` or `gh repo list`)."},
                        "targetNode": {"type": "string", "description": "Target computer: `laptop` or `desktop` (default `laptop`)."},
                        "cwd": {"type": "string", "description": "Optional working directory (default: this machine's repos folder)."},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_sync_repo",
                "description": "Keep a repo in sync across machines: action=handoff brings the other machine's newer/uncommitted work to targetNode (safe, never merges); pull fast-forwards clean repos; reconcile delegates divergence to AGY or Claude.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repoName": {"type": "string", "description": "Repository name in the repos folder."},
                        "action": {"type": "string", "enum": ["handoff", "pull", "reconcile"]},
                        "targetNode": {"type": "string", "description": "laptop, desktop, or both"},
                        "agent": {"type": "string", "description": "agy-cli or claude-cli when action=reconcile"},
                    },
                    "required": ["repoName"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_delegate_agent",
                "description": "Invoke AGY (`agy-cli`) or Claude Code (`claude-cli`) with --dangerously-skip-permissions on Laptop or Desktop (gracefully falls back to Laptop if Desktop is offline).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "enum": ["agy-cli", "claude-cli"]},
                        "prompt": {"type": "string", "description": "Task or question for AGY or Claude."},
                        "targetNode": {"type": "string", "description": "auto, laptop, or desktop"},
                        "cwd": {"type": "string", "description": "Optional working directory (e.g. ~/GitHub/my-app)."},
                        "model": {"type": "string", "description": "Optional AGY or Claude model override."},
                    },
                    "required": ["agent", "prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orch_vault_manage",
                "description": "List, read, write, or copy plans/files inside ~/LLM-Vault/ shared storage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "read", "write", "copy"]},
                        "relPath": {"type": "string", "description": "Relative path inside ~/LLM-Vault/ (e.g. plans/app.md)."},
                        "content": {"type": "string", "description": "File text when action=write."},
                        "sourcePath": {"type": "string", "description": "Absolute source file path when action=copy."},
                    },
                    "required": ["action"],
                },
            },
        },
    ]


def format_task_routing_summary(settings: dict[str, Any]) -> str:
    """Render an unambiguous summary of the user's current Task Routing & Engine Settings."""
    engines = settings.get("engines") or {}
    orch_cfg = engines.get("orchestrator") or {}
    agy_cfg = engines.get("agy") or {}
    claude_cfg = engines.get("claude") or {}
    tdefs = settings.get("taskDefaults") or {}

    lines = [
        f"- Primary Orchestrator LLM (you): `{orch_cfg.get('modelId', 'qwen3-8b')}` on node `{orch_cfg.get('nodeId', 'laptop')}`",
        f"- AGY Default Engine: model=`{agy_cfg.get('model', 'gemini-4-neon-high')}`, effort=`{agy_cfg.get('effort', 'high')}`, mode=`{agy_cfg.get('mode', 'accept-edits')}`, --dangerously-skip-permissions={agy_cfg.get('dangerouslySkipPermissions', False)}",
        f"- Claude Default Engine: model=`{claude_cfg.get('model', 'opus')}`, fallback=`{claude_cfg.get('fallbackModel', 'sonnet')}`, effort=`{claude_cfg.get('effort', 'high')}`, --dangerously-skip-permissions={claude_cfg.get('dangerouslySkipPermissions', False)}",
        "- User's Configured Specialist Task Routing (`taskDefaults`):",
    ]
    for tk, td in tdefs.items():
        if not isinstance(td, dict):
            continue
        mid = td.get("modelId", "")
        nid = td.get("nodeId", "auto")
        lbl = td.get("label", tk)
        extra = ""
        if mid in ("agy-cli", "claude-cli", "agy", "claude"):
            em = td.get("engineModel") or (agy_cfg.get("model") if "agy" in mid else claude_cfg.get("model"))
            ef = td.get("effort") or (agy_cfg.get("effort") if "agy" in mid else claude_cfg.get("effort"))
            extra = f" (engineModel=`{em}`, thinking=`{ef}`)"
        lines.append(f"  - `{tk}` ({lbl}): model=`{mid}`{extra}, targetNode=`{nid}`")
    return "\n".join(lines)


def build_orchestrator_system_prompt(
    base_prompt: str,
    conn: sqlite3.Connection,
    settings: dict[str, Any],
    desktop_online: bool,
    tools_reg: dict[str, Any],
) -> str:
    """Append real passive capability, node status, AND user's exact configured Settings."""
    nid = str(settings["node"]["id"])
    role = str(settings["node"].get("role") or "hub")
    local_tools = read_tool_inventory(conn, nid)
    if not local_tools:
        llm_dir, _, _ = model_dirs(settings)
        ggufs = [f.name for f in sorted(llm_dir.glob("*.gguf"))] if llm_dir and llm_dir.is_dir() else []
    else:
        ggufs = [t["name"] for t in local_tools if t["kind"] == "llm"]

    clis = []
    if resolve_cli_binary("agy"):
        clis.append("agy-cli (AGY)")
    if resolve_cli_binary("claude"):
        clis.append("claude-cli (Claude Code)")

    queued_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM desktop_intermission_queue WHERE status='queued'"
    ).fetchone()["c"]
    repo_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM repo_inventory WHERE node_id=?", (nid,)
    ).fetchone()["c"]

    py_names = [k for k, v in tools_reg.items() if v.get("enabled")]
    desktop_rows = read_tool_inventory(conn, "desktop")
    desktop_llms = [t["name"] for t in desktop_rows if t["kind"] == "llm"]
    desktop_clis = [t["name"] for t in desktop_rows if t["kind"] == "cli_agent"]
    desktop_repo_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM repo_inventory WHERE node_id='desktop'"
    ).fetchone()["c"]
    lines = [
        base_prompt,
        "",
        "## Live Orchestrator Environment & User Settings",
        f"- Active Node: `{nid}` (role: {role}) | Desktop Worker: {'ONLINE' if desktop_online else 'OFFLINE (Laptop Fallback active)'}",
        f"- Laptop CLI Agents: {', '.join(clis) or 'none'}",
        f"- Laptop GGUF/Router LLMs ({len(ggufs)}): {', '.join(ggufs[:25]) or 'inspect via orch_list_capabilities'}",
        f"- Desktop Worker Ollama/Router LLMs ({len(desktop_llms)}): {', '.join(desktop_llms[:25]) or ('none cached' if not desktop_online else 'inspect via orch_list_capabilities')}",
        f"- Desktop Worker CLI Agents ({len(desktop_clis)}): {', '.join(desktop_clis) or 'none cached'}",
        f"- Python Suite Tools: {', '.join(py_names) or 'none'}",
        f"- Indexed GitHub Repos: laptop={repo_cnt}, desktop={desktop_repo_cnt} | Pending Desktop Intermission Queue: {queued_cnt}",
        f"- Shared Hub Vault: {settings['paths'].get('vaultDir', '~/LLM-Vault')}",
        "",
        "## User's Configured Task Routing & Engine Settings",
        format_task_routing_summary(settings),
        "",
        "IMPORTANT INSTRUCTIONS:",
        "1. You are the Orchestrator. You talk directly with the user and coordinate specialist LLMs/agents (`agy-cli`, `claude-cli`, or local GGUFs) on their behalf.",
        "2. Always respect the user's configured Specialist Task Routing table above. For example, when asked which model is set for Story Writing, Coding, Reasoning, Research, Image, Video, or Music, state the exact configured model/node from the list above (or verify via `orch_get_settings`). Do NOT substitute Claude or another model unless configured there.",
        "3. Use `orch_list_capabilities` to passively inspect available models/tools without waking GPUs; for coding, reasoning, research or story work call `orch_run_specialist_llm` with that taskType so it goes to the user's configured model or agent (pass `repo` for anything about code); use `orch_delegate_agent` only to pick a specific agent yourself; use `orch_get_repo_status` / `orch_sync_repo` for Laptop, Desktop, and GitHub Remote repos; use `orch_run_command` to run `git` or `gh` commands on Laptop or Desktop; use `orch_vault_manage` for ~/LLM-Vault.",
    ]
    return "\n".join(lines)


def list_github_remote_repos(limit: int = 250) -> dict[str, Any]:
    """Query the user's GitHub remote repositories via `gh repo list`."""
    try:
        proc = subprocess.run(
            [
                "gh",
                "repo",
                "list",
                "--limit",
                str(max(1, min(limit, 500))),
                "--json",
                "name,isPrivate,defaultBranchRef,pushedAt,url",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return {"available": False, "repos": [], "error": "`gh` CLI is not installed."}
    except subprocess.TimeoutExpired:
        return {"available": False, "repos": [], "error": "`gh repo list` timed out."}
    except Exception as exc:
        return {"available": False, "repos": [], "error": str(exc)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"gh exited {proc.returncode}"
        return {"available": False, "repos": [], "error": err[:300]}

    try:
        raw = json.loads(proc.stdout or "[]")
    except Exception as exc:
        return {"available": False, "repos": [], "error": f"Invalid gh JSON: {exc}"}

    repos = [
        {
            "name": str(item.get("name") or ""),
            "private": bool(item.get("isPrivate")),
            "defaultBranch": str(((item.get("defaultBranchRef") or {}).get("name")) or ""),
            "pushedAt": str(item.get("pushedAt") or ""),
            "url": str(item.get("url") or ""),
        }
        for item in raw
        if item.get("name")
    ]
    return {"available": True, "repos": repos, "error": None}


async def execute_builtin_tool(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    settings: dict[str, Any],
    node_id: str,
    github_dir: Path,
    vault_dir: Path,
    desktop_online: bool,
    tools_reg: dict[str, Any],
    dispatch_cli_agent: Callable[..., Awaitable[dict[str, Any]]],
    sync_repo_fn: Callable[[str, str, str, str], Awaitable[dict[str, Any]]],
    run_specialist_llm_fn: Callable[..., Awaitable[dict[str, Any]]],
    run_command_fn: Callable[[str, str, str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Execute one of the built-in orchestration tools asynchronously."""
    if name == "orch_get_settings":
        engines = settings.get("engines") or {}
        return {
            "orchestrator": engines.get("orchestrator") or {},
            "agy": engines.get("agy") or {},
            "claude": engines.get("claude") or {},
            "taskDefaults": settings.get("taskDefaults") or {},
            "desktopOnline": desktop_online,
            "peers": settings.get("peers") or [],
        }

    if name == "orch_run_specialist_llm":
        prompt = str(args.get("prompt") or "")
        task_type = str(args.get("taskType") or "coding")
        model_id = str(args.get("modelId") or "")
        target_node = str(args.get("targetNode") or "auto")
        return await run_specialist_llm_fn(task_type, model_id, target_node, prompt, str(args.get("repo") or args.get("cwd") or ""))

    if name == "orch_list_capabilities":
        refresh = bool(args.get("refresh", False))
        llm_dir, comfy_dir, _ = model_dirs(settings)
        gguf_files = [f.name for f in sorted(llm_dir.glob("*.gguf"))] if llm_dir and llm_dir.is_dir() else []
        comfy_dir = comfy_dir or Path("/nonexistent")
        image_models = (
            [f.name for f in sorted((comfy_dir / "checkpoints").glob("*.safetensors"))] if (comfy_dir / "checkpoints").is_dir() else []
        ) + (
            [f.name for f in sorted((comfy_dir / "unet").glob("*.gguf"))] if (comfy_dir / "unet").is_dir() else []
        )
        video_models = (
            [f.name for f in sorted((comfy_dir / "diffusion_models").glob("*.safetensors"))]
            if (comfy_dir / "diffusion_models").is_dir()
            else []
        )
        if refresh:
            discovered = await discover_node_models(settings, node_id)
            sync_local_tool_inventory(conn, node_id, discovered, tools_reg, vault_dir)
        cached_tools = read_tool_inventory(conn)

        computers: dict[str, dict[str, Any]] = {}
        for t in cached_tools:
            cnode = computers.setdefault(
                t["nodeId"],
                {
                    "online": True if t["nodeId"] == node_id else desktop_online,
                    "llms": [],
                    "cliAgents": [],
                    "imageModels": [],
                    "videoModels": [],
                    "pythonTools": [],
                },
            )
            kind = t["kind"]
            if kind == "llm":
                cnode["llms"].append(t["name"])
            elif kind == "cli_agent":
                cnode["cliAgents"].append(t["name"])
            elif kind == "image":
                cnode["imageModels"].append(t["name"])
            elif kind == "video":
                cnode["videoModels"].append(t["name"])
            elif kind == "python_plugin":
                cnode["pythonTools"].append(t["name"])

        if node_id not in computers:
            computers[node_id] = {
                "online": True,
                "llms": gguf_files,
                "cliAgents": [x for x in ("agy-cli" if resolve_cli_binary("agy") else "", "claude-cli" if resolve_cli_binary("claude") else "") if x],
                "imageModels": image_models,
                "videoModels": video_models,
                "pythonTools": [k for k, v in tools_reg.items() if v.get("enabled")],
            }

        compact_routing = {
            tk: {
                "model": td.get("modelId"),
                "engineModel": td.get("engineModel"),
                "thinking": td.get("effort"),
                "node": td.get("nodeId"),
            }
            for tk, td in (settings.get("taskDefaults") or {}).items()
            if isinstance(td, dict)
        }
        return {
            "activeHub": node_id,
            "desktopOnline": desktop_online,
            "computers": computers,
            "configuredRouting": compact_routing,
            "localGgufModelsOnDisk": gguf_files,
            "agySubModels": [m["id"] for m in list_agy_models()],
            "claudeAliases": ["opus", "sonnet", "haiku"],
            "note": "Read passively from SQLite DB & filesystem without waking or loading any GPU model.",
        }

    if name == "orch_get_repo_status":
        refresh = bool(args.get("refresh", False))
        include_remote = bool(args.get("includeRemote", True))
        if refresh:
            scan_local_repos(conn, github_dir, node_id)
        cmp_rows = compare_repo_inventories(read_repo_inventory(conn))
        remote_info = list_github_remote_repos() if include_remote else {"available": False, "repos": [], "error": None}
        remote_by_lower = {r["name"].lower(): r for r in remote_info["repos"]}

        filt = str(args.get("repoName") or "").strip().lower()
        queue = list_intermission_queue(conn, limit=15)
        if filt:
            matched = [r for r in cmp_rows if filt in r["repoName"].lower()]
            detailed = [
                {
                    "repoName": r["repoName"],
                    "status": r["status"],
                    "laptop": r.get("laptop"),
                    "desktop": r.get("desktop"),
                    "githubRemote": remote_by_lower.get(r["repoName"].lower()),
                }
                for r in matched[:15]
            ]
            return {
                "desktopOnline": desktop_online,
                "totalMatched": len(matched),
                "repos": detailed,
                "remoteError": remote_info["error"],
                "pendingIntermissionQueue": [q for q in queue if q["status"] != "synced"][:10],
            }

        laptop_names = sorted(r["repoName"] for r in cmp_rows if r.get("laptop"))
        desktop_names = sorted(r["repoName"] for r in cmp_rows if r.get("desktop"))
        remote_names = sorted(r["name"] for r in remote_info["repos"])
        local_lower = {r["repoName"].lower() for r in cmp_rows}

        out_of_sync = []
        for r in cmp_rows:
            if r["status"] == "in_sync":
                continue
            lap = r.get("laptop")
            desk = r.get("desktop")
            out_of_sync.append({
                "repo": r["repoName"],
                "status": r["status"],
                "laptop": f"{lap['branch']} d={lap['dirtyCount']} +{lap['ahead']}/-{lap['behind']}" if lap else None,
                "desktop": f"{desk['branch']} d={desk['dirtyCount']}" if desk else None,
            })

        remote_only = sorted(r["name"] for r in remote_info["repos"] if r["name"].lower() not in local_lower) if remote_info["available"] else []
        local_not_on_remote = (
            sorted(r["repoName"] for r in cmp_rows if r["repoName"].lower() not in remote_by_lower)
            if remote_info["available"]
            else []
        )

        return {
            "desktopOnline": desktop_online,
            "counts": {
                "laptopTotal": len(laptop_names),
                "desktopTotal": len(desktop_names),
                "githubRemoteTotal": len(remote_names) if remote_info["available"] else None,
                "laptopDesktopMatchedClean": sum(1 for r in cmp_rows if r["status"] == "in_sync"),
                "outOfSyncLaptopVsDesktopOrRemote": len(out_of_sync),
                "remoteOnly": len(remote_only),
                "localNotOnRemote": len(local_not_on_remote),
            },
            "outOfSyncRepos": out_of_sync[:50],
            "remoteOnlyRepos": remote_only[:50],
            "localNotOnRemoteRepos": local_not_on_remote[:50],
            "pendingIntermissionQueue": [q for q in queue if q["status"] != "synced"][:10],
            "remoteError": remote_info["error"],
            "laptopRepos": laptop_names,
            "desktopRepos": desktop_names,
            "githubRemoteRepos": remote_names,
        }

    if name == "orch_run_command":
        if not run_command_fn:
            return {"error": "Command runner is unavailable in this context."}
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return {"error": "Missing required `command` parameter."}
        tnode = str(args.get("targetNode") or node_id or "laptop")
        cwd = str(args.get("cwd") or str(github_dir))
        res = await run_command_fn(tnode, cmd, cwd)
        out = str(res.get("output") or "")
        return {
            "executedNode": res.get("executedNode", tnode),
            "exitCode": res.get("exitCode"),
            "truncated": len(out) > 4000,
            "output": out[-4000:],
            "error": res.get("error"),
        }

    if name == "orch_sync_repo":
        repo_name = str(args.get("repoName") or "")
        action = str(args.get("action") or "pull")
        target_node = str(args.get("targetNode") or "both")
        agent = str(args.get("agent") or "agy-cli")
        return await sync_repo_fn(repo_name, action, target_node, agent)

    if name == "orch_delegate_agent":
        agent = str(args.get("agent") or "agy-cli")
        prompt = str(args.get("prompt") or "")
        target_node = str(args.get("targetNode") or "auto")
        cwd = str(args.get("cwd") or str(github_dir))
        model = str(args.get("model") or "")
        return await dispatch_cli_agent(
            target_node=target_node,
            agent_kind=agent,
            prompt=prompt,
            cwd=cwd,
            model=model,
        )

    if name == "orch_vault_manage":
        action = str(args.get("action") or "list")
        vault_dir.mkdir(parents=True, exist_ok=True)
        if action == "list":
            return {"vaultDir": str(vault_dir), "files": list_vault_files(vault_dir)[:50]}
        rel = str(args.get("relPath") or "")
        if action == "read":
            target = resolve_vault_path(vault_dir, rel)
            if not target.is_file():
                return {"error": f"Vault file not found: {rel}"}
            return {"relPath": rel, "content": target.read_text(encoding="utf-8", errors="replace")[:12000]}
        if action == "write":
            target = resolve_vault_path(vault_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args.get("content") or ""), encoding="utf-8")
            return {"ok": True, "relPath": rel, "path": str(target)}
        if action == "copy":
            src = Path(str(args.get("sourcePath") or "")).expanduser()
            if not src.is_file():
                return {"error": f"Source file not found: {src}"}
            dest_rel = rel or src.name
            target = resolve_vault_path(vault_dir, dest_rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            return {"ok": True, "relPath": dest_rel, "path": str(target)}
        return {"error": f"Unsupported vault action: {action}"}

    return {"error": f"Unknown builtin tool: {name}"}
