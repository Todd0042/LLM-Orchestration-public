"""Configuration, Hub/Worker defaults, engine presets, model strength catalog, and persistent SQLite store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("LLM_ORCH_ENV_FILE", str(ROOT_DIR / "llm-orchestration.env")))


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read this machine's settings (KEY=value lines, see llm-orchestration.env.example) into the environment.

    Values already in the environment win, so systemd or a shell can still override a line. ``~`` and
    ``$VARS`` in values are expanded.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, os.path.expanduser(os.path.expandvars(value)))


load_env_file()


def env_path(name: str, default: Path | str = "") -> str:
    """A folder or file setting from the environment, with ``~`` expanded; ``default`` if unset."""
    v = os.environ.get(name, "").strip()
    return str(Path(v).expanduser()) if v else str(default)
CONFIG_DIR = Path(os.environ.get("LLM_ORCH_CONFIG_DIR", str(Path.home() / ".config" / "llm-orchestration")))
DATA_DIR = Path(os.environ.get("LLM_ORCH_DATA_DIR", str(Path.home() / ".local" / "share" / "llm-orchestration")))
ARTIFACTS_DIR = DATA_DIR / "artifacts"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
TOKEN_FILE = CONFIG_DIR / "node_token"
DB_FILE = DATA_DIR / "orchestration.db"


TASK_KEYS = (
    "coding",
    "reasoning",
    "story",
    "research",
    "peer_relay",
    "image",
    "video",
    "music",
)


def default_github_dir() -> Path:
    env_dir = os.environ.get("LLM_ORCH_GITHUB_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    cand1 = Path.home() / "GitHub"
    cand2 = Path.home() / "Documents" / "GitHub"
    if not cand1.is_dir() and cand2.is_dir():
        return cand2
    return cand1


def default_settings() -> dict[str, Any]:
    host_name = socket.gethostname() or "node"
    is_laptop = (
        "laptop" in host_name.lower()
        or Path("/sys/class/power_supply/BAT0").exists()
        or Path("/sys/class/power_supply/BAT1").exists()
    )
    node_id = os.environ.get("LLM_ORCH_NODE_ID") or ("laptop" if is_laptop else "desktop")
    role = os.environ.get("LLM_ORCH_ROLE") or ("hub" if node_id == "laptop" else "worker")
    return {
        "node": {
            "id": node_id,
            "displayName": "Laptop" if node_id == "laptop" else ("Desktop" if node_id == "desktop" else host_name),
            "role": role,
            "port": int(os.environ.get("LLM_ORCH_PORT", "8790")),
            "mdnsEnabled": True,
            "preferDesktopGpu": True,
            "hubUrl": os.environ.get("LLM_ORCH_HUB_URL", ""),
            "tunnelPort": int(os.environ.get("LLM_ORCH_TUNNEL_PORT", "8791")),
            "bindHost": os.environ.get("LLM_ORCH_BIND_HOST", "0.0.0.0"),
            "alwaysOn": True,
        },
        "peers": [],
        "paths": {
            "agyDir": str(Path.home() / ".gemini" / "antigravity-cli"),
            "claudeDir": str(Path.home() / ".claude"),
            "toolsDir": str(ROOT_DIR / "tools"),
            "githubDir": str(default_github_dir()),
            "vaultDir": str(Path.home() / "LLM-Vault"),
            "llmBaseUrl": os.environ.get("LLM_ORCH_LLM_BASE", "http://127.0.0.1:8080/v1"),
            "comfyUrl": os.environ.get("LLM_ORCH_COMFY_URL", "http://127.0.0.1:8188"),
            "gpuSchedulerUrl": os.environ.get("LLM_ORCH_GPU_SCHEDULER_URL", "http://127.0.0.1:11436"),
            "useGpuScheduler": os.environ.get("LLM_ORCH_USE_GPU_SCHEDULER", "0") in ("1", "true", "yes"),
            "backupsDir": env_path("LLM_ORCH_BACKUPS_DIR", Path.home() / "Backups" / "llm-orchestration"),
            # Optional: local model folders shown as capabilities when no model server answers.
            "llmModelsDir": env_path("LLM_ORCH_LLM_MODELS_DIR"),
            "comfyModelsDir": env_path("LLM_ORCH_COMFY_MODELS_DIR"),
            "comfyLauncher": env_path("LLM_ORCH_COMFY_LAUNCHER"),
        },
        "security": {
            "publicHosts": [h.strip() for h in os.environ.get("LLM_ORCH_PUBLIC_HOSTS", "").split(",") if h.strip()],
            "allowedHosts": [],
            "allowedHostSuffixes": [".ts.net"],
            "allowedOrigins": [],
            "allowDevOrigins": True,
            "extraReadRoots": [],
        },
        "alerts": {
            "ntfyUrl": os.environ.get("LLM_ORCH_NTFY_URL", ""),
            "minIntervalSec": 300,
        },
        "failover": {
            "enabled": False,
            "role": "none",
            "primaryNode": "desktop",
            "publicProbeUrl": os.environ.get("LLM_ORCH_PUBLIC_PROBE_URL", ""),
            "downAfterSec": 120,
            "upAfterSec": 180,
            "connectorUnit": os.environ.get("LLM_ORCH_FAILOVER_UNIT", ""),
            "internetProbeUrl": "https://www.cloudflare.com/cdn-cgi/trace",
        },
        "monitor": {"intervalSec": 30, "failThreshold": 3, "maxRestartsPerHour": 4, "backoffBaseSec": 60, "backoffMaxSec": 1800},
        "backups": {"targetNode": "", "intervalSec": 21600, "keep": 14},
        "replication": {"intervalSec": 20},
        "repoSync": {"autoHandoff": True, "pushWipToGitHub": True},
        "services": [],
        "auth": {
            "totpStateDir": env_path("LLM_ORCH_TOTP_DIR", DATA_DIR),
            "trustCloudflareHeaders": True,
            "sessionIdleSec": int(os.environ.get("LLM_ORCH_SESSION_IDLE_SEC", "3600")),
            "requireAuthEverywhere": False,
        },
        "engines": {
            "orchestrator": {
                "nodeId": node_id,
                "modelId": "qwen3-8b",
                "engineModel": "",
                "effort": "high",
                "mode": "accept-edits",
                "temperature": 0.25,
                "maxTokens": 4096,
            },
            "agy": {
                "model": "gemini-4-neon-high",
                "effort": "high",
                "mode": "accept-edits",
                "dangerouslySkipPermissions": os.environ.get("LLM_ORCH_AGENT_SKIP_PERMISSIONS", "0") in ("1", "true", "yes"),
            },
            "claude": {
                "model": "opus",
                "fallbackModel": "sonnet",
                "effort": "high",
                "dangerouslySkipPermissions": os.environ.get("LLM_ORCH_AGENT_SKIP_PERMISSIONS", "0") in ("1", "true", "yes"),
            },
            "local": {
                "enableThinking": True,
                "temperature": 0.2,
                "maxTokens": 4096,
                "systemPrompt": "",
            },
        },
        "terminal": {
            "shell": "/usr/bin/bash",
            "askApprovalBeforeRun": True,
            "sudoTimeoutSec": 60,
            "trustedReadOnlyAutoRun": True,
            "approvalTimeoutSec": 300,
            "agentTimeoutSec": 3600,
            "readOnlyAllowlist": [],
        },
        "taskDefaults": {
            "coding": {
                "label": "Coding & Diffs",
                "nodeId": "auto",
                "modelId": "qwen3-8b",
                "temperature": 0.15,
                "maxTokens": 2048,
                "systemPrompt": "You are a precise software engineering agent. Propose clean patches, cite exact file paths, and verify changes.",
                "chain": ["claude-cli", "agy-cli", "qwen3-8b"],
            },
            "reasoning": {
                "label": "Deep Reasoning",
                "nodeId": "auto",
                "modelId": "qwen3-14b",
                "temperature": 0.2,
                "maxTokens": 2560,
                "systemPrompt": "Think carefully step-by-step, surface edge cases and tradeoffs clearly, and give a concise actionable conclusion.",
                "chain": ["claude-cli", "agy-cli", "qwen3-14b"],
            },
            "story": {
                "label": "Story Writing",
                "nodeId": "auto",
                "modelId": "dolphin-llama3:8B",
                "temperature": 0.72,
                "maxTokens": 2400,
                "systemPrompt": "You are a vivid creative writer and dialogue director focused on character voice, pacing, and narrative continuity.",
            },
            "research": {
                "label": "Info Gathering & Tools",
                "nodeId": "auto",
                "modelId": "qwen3-8b",
                "temperature": 0.2,
                "maxTokens": 1800,
                "systemPrompt": "Use available info-gathering tools systematically, cite file paths and URLs, and summarize verified findings.",
                "chain": ["claude-cli", "agy-cli", "qwen3-8b"],
            },
            "peer_relay": {
                "label": "Desktop ↔ Laptop Self-Chat",
                "nodeId": "auto",
                "modelId": "agy-cli",
                "temperature": 0.35,
                "maxTokens": 1600,
                "systemPrompt": "Collaborate with the peer agent cleanly. End with [DONE] when the goal is complete.",
            },
            "image": {
                "label": "Image Generation",
                "nodeId": "auto",
                "modelId": "flux1-schnell-Q4_K_S.gguf",
                "temperature": 0.0,
                "maxTokens": 512,
                "systemPrompt": "flux-schnell 4-step workflow",
            },
            "video": {
                "label": "Video Generation",
                "nodeId": "auto",
                "modelId": "wan2.1_t2v_1.3B_bf16.safetensors",
                "temperature": 0.0,
                "maxTokens": 512,
                "systemPrompt": "wan-video workflow",
            },
            "music": {
                "label": "Music Generation",
                "nodeId": "auto",
                "modelId": "music_studio",
                "temperature": 0.0,
                "maxTokens": 512,
                "systemPrompt": "python music tool",
            },
        },
        "modelOverrides": {},
        "disabledTools": [],
        "autoRouteByTask": True,
    }


def infer_model_profile(model_id: str, kind: str = "llm") -> dict[str, Any]:
    """Return human-readable label, strength badges, and fit note from model ID."""
    low = model_id.lower()
    if kind == "image":
        if "flux" in low:
            strengths = ["Image", "Fast"] if "schnell" in low or "klein" in low else ["Image", "Photoreal"]
        elif "animagine" in low or "illustrious" in low or "pony" in low:
            strengths = ["Image", "Stylized / Anime"]
        elif "qwen-image" in low:
            strengths = ["Image", "Typography"]
        else:
            strengths = ["Image", "SDXL"]
        return {
            "displayName": model_id.replace(".safetensors", "").replace(".gguf", ""),
            "strengths": strengths,
            "fit": "8 GB VRAM (ComfyUI)",
        }
    if kind == "video":
        return {
            "displayName": model_id.replace(".safetensors", ""),
            "strengths": ["Video", "Fast 1.3B" if "1.3b" in low else "High Quality 5B"],
            "fit": "8 GB VRAM (Exclusive Turn)",
        }
    if kind == "music":
        return {
            "displayName": model_id,
            "strengths": ["Music", "Python Suite"],
            "fit": "Python Tool Worker",
        }

    if kind == "embed" or "embed" in low:
        return {
            "displayName": model_id.replace(":latest", ""),
            "strengths": ["Embedding"],
            "fit": "Vector Embeddings Only",
        }
    if low in ("agy", "agy-cli"):
        return {
            "displayName": "AGY Agent (CLI)",
            "strengths": ["AGY Agent", "Coding", "Reasoning", "Tools"],
            "fit": "Workspace Agent (--dangerously-skip-permissions)",
        }
    if low in ("claude", "claude-cli"):
        return {
            "displayName": "Claude Code (CLI)",
            "strengths": ["Claude Code", "Coding", "Architecture"],
            "fit": "Anthropic CLI (--dangerously-skip-permissions)",
        }

    if "qwen2.5-coder:14b" in low:
        return {
            "displayName": "Qwen2.5 Coder 14B",
            "strengths": ["Recommended", "Coding", "Tools"],
            "fit": "9.0 GB · Top Local Orchestrator",
        }
    if "gpt-oss:20b" in low:
        return {
            "displayName": "GPT-OSS 20B (MXFP4)",
            "strengths": ["Recommended", "Tools", "MoE"],
            "fit": "13.8 GB · Fast Tool-Calling MoE",
        }
    if "qwen3:30b-a3b" in low:
        return {
            "displayName": "Qwen3 30B-A3B (Q4_K_M)",
            "strengths": ["Recommended", "Reasoning", "MoE"],
            "fit": "18.6 GB · High Precision 30B MoE",
        }
    if "qwen3-30b-a3b" in low:
        return {
            "displayName": "Qwen3 30B-A3B (Q2_K)",
            "strengths": ["Reasoning", "2-bit"],
            "fit": "12 GB · 8K RAM Spill",
        }
    if "coder" in low:
        return {"displayName": "Qwen2.5 Coder 7B" if "2.5" in low else model_id, "strengths": ["Coding", "Tools"], "fit": "Code & Diffs"}
    if "dolphin-x1-trinity" in low:
        return {"displayName": "Dolphin-X1 Trinity Nano (Q5_K_M)", "strengths": ["Story", "Uncensored"], "fit": "4.4 GB · Fast Creative"}
    if "dolphin-x1" in low:
        return {"displayName": "Dolphin-X1 8B (Q5_K_M)", "strengths": ["Story", "Uncensored"], "fit": "5.7 GB · Creative Prose"}
    if "dolphin3" in low:
        return {"displayName": "Dolphin 3 8B", "strengths": ["Story", "Uncensored"], "fit": "4.9 GB · Creative Prose"}
    if "dolphin" in low or "gemma" in low or "nemo" in low or "stheno" in low or "wayfarer" in low:
        name_clean = "Dolphin Llama3 8B" if "dolphin-llama3" in low else model_id
        strengths = ["Story", "Uncensored"] if "dolphin" in low else ["Story", "Dialogue"]
        return {"displayName": name_clean, "strengths": strengths, "fit": "Creative Prose"}
    if "saul" in low:
        return {"displayName": "Saul 7B", "strengths": ["Legal", "Compliance"], "fit": "5.6 GB · 16K Legal Audit"}
    if "qwen3-8b" in low or "qwen3:8b" in low:
        return {"displayName": "Qwen3 8B", "strengths": ["Hub Fallback", "Coding", "Tools"], "fit": "5.5 GB · 16K Daily Driver"}
    if "qwen3-14b" in low or "qwen3:14b" in low:
        return {"displayName": "Qwen3 14B", "strengths": ["Recommended", "Reasoning", "Coding"], "fit": "7.6 GB · 16K Strong Local"}
    if "qwen2.5:7b" in low:
        return {"displayName": "Qwen2.5 7B", "strengths": ["Balanced", "Tools"], "fit": "4.7 GB · Fast General"}
    if "llama3.1:8b" in low:
        return {"displayName": "Llama 3.1 8B", "strengths": ["General", "Tools"], "fit": "4.9 GB · Fast General"}
    if "mistral:7b" in low:
        return {"displayName": "Mistral 7B", "strengths": ["General"], "fit": "4.4 GB · Fast General"}
    if "r1" in low or "reasoning" in low or "phi-4" in low or "30b" in low:
        strengths = ["Reasoning", "Coding"] if "qwen" in low else ["Reasoning"]
        fit = "Deep Logic / 15 GB RAM+8 GB VRAM" if ("14b" in low or "30b" in low) else "Fast Thinking"
        return {"displayName": model_id, "strengths": strengths, "fit": fit}
    return {"displayName": model_id, "strengths": ["General"], "fit": "Local Model"}


def ensure_node_token(config_dir: Path = CONFIG_DIR) -> str:
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    tok_path = config_dir / "node_token"
    if tok_path.is_file():
        tok = tok_path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    tok_path.write_text(tok + "\n", encoding="utf-8")
    os.chmod(tok_path, stat.S_IRUSR | stat.S_IWUSR)
    return tok


def load_settings(settings_file: Path = SETTINGS_FILE) -> dict[str, Any]:
    base = default_settings()
    if not settings_file.is_file():
        save_settings(base, settings_file)
        return base
    try:
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        for k in ("node", "paths", "auth", "engines", "terminal", "taskDefaults", "security", "alerts", "failover", "monitor", "backups", "replication", "repoSync"):
            if isinstance(saved.get(k), dict):
                if k in ("taskDefaults", "engines"):
                    for tk, tv in saved[k].items():
                        if isinstance(tv, dict) and tk in base[k]:
                            base[k][tk].update(tv)
                else:
                    base[k].update(saved[k])
        for k in ("peers", "modelOverrides", "disabledTools", "autoRouteByTask", "services"):
            if k in saved:
                base[k] = saved[k]
    except Exception:
        pass
    return base


def save_settings(settings: dict[str, Any], settings_file: Path = SETTINGS_FILE) -> None:
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(settings_file)


SETTINGS_SECTIONS = (
    "node", "peers", "paths", "auth", "security", "alerts", "failover", "services", "engines",
    "monitor", "backups", "replication", "repoSync",
    "terminal", "taskDefaults", "modelOverrides", "disabledTools", "autoRouteByTask",
)


class SettingsError(ValueError):
    pass


def _check_like(template: Any, value: Any, where: str) -> None:
    """Values must keep the type of the default; unknown keys in fixed-shape sections are rejected."""
    if template is None:
        return
    if isinstance(template, bool):
        if not isinstance(value, bool):
            raise SettingsError(f"{where} must be true/false")
    elif isinstance(template, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{where} must be a number")
    elif isinstance(template, str):
        if not isinstance(value, str) or len(value) > 20_000:
            raise SettingsError(f"{where} must be a string")
    elif isinstance(template, list):
        if not isinstance(value, list):
            raise SettingsError(f"{where} must be a list")
    elif isinstance(template, dict):
        if not isinstance(value, dict):
            raise SettingsError(f"{where} must be an object")
        for k, v in value.items():
            if k in template:
                _check_like(template[k], v, f"{where}.{k}")


def merge_settings_update(current: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Return a validated copy of current with body's sections applied (sections merged, not replaced)."""
    if not isinstance(body, dict):
        raise SettingsError("settings body must be an object")
    unknown = [k for k in body if k not in SETTINGS_SECTIONS]
    if unknown:
        raise SettingsError(f"unknown settings sections: {', '.join(unknown)}")
    template = default_settings()
    out = json.loads(json.dumps(current))
    for k, v in body.items():
        _check_like(template.get(k), v, k)
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("modelOverrides",):
            for sk, sv in v.items():
                if isinstance(sv, dict) and isinstance(out[k].get(sk), dict):
                    out[k][sk] = {**out[k][sk], **sv}
                else:
                    out[k][sk] = sv
        else:
            out[k] = v
    for p in out.get("peers") or []:
        if not isinstance(p, dict) or not str(p.get("id") or "").strip():
            raise SettingsError("each peer needs an id")
        urls = [p.get("url")] + list(p.get("urls") or [])
        for u in [u for u in urls if u]:
            if not str(u).startswith(("http://", "https://")):
                raise SettingsError(f"peer url must be http(s): {u}")
    return out


def init_db(db_path: Path = DB_FILE) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'solo',
            task_type TEXT NOT NULL DEFAULT 'coding',
            node_id TEXT NOT NULL DEFAULT 'desktop',
            peer_config_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            sender_label TEXT NOT NULL DEFAULT '',
            node_id TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            events_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            node_id TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS repo_inventory (
            node_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            path TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            upstream_remote TEXT NOT NULL DEFAULT '',
            ahead INTEGER NOT NULL DEFAULT 0,
            behind INTEGER NOT NULL DEFAULT 0,
            dirty_count INTEGER NOT NULL DEFAULT 0,
            untracked_count INTEGER NOT NULL DEFAULT 0,
            branches_json TEXT NOT NULL DEFAULT '[]',
            dirty_json TEXT NOT NULL DEFAULT '[]',
            last_commit_at TEXT NOT NULL DEFAULT '',
            scanned_at REAL NOT NULL,
            PRIMARY KEY (node_id, repo_name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_inventory (
            node_id TEXT NOT NULL,
            tool_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL,
            PRIMARY KEY (node_id, tool_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS desktop_intermission_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            category TEXT NOT NULL,
            target TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            result_note TEXT NOT NULL DEFAULT '',
            synced_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            progress_text TEXT NOT NULL DEFAULT '',
            node_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.commit()
    from .db import migrate

    migrate(conn)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def model_dirs(settings: dict[str, Any]) -> tuple[Path | None, Path | None, Path | None]:
    """(llm_models_dir, comfy_models_dir, comfy_launcher) from settings; None for any that isn't set."""
    paths = settings.get("paths") or {}
    def opt(key: str) -> Path | None:
        v = str(paths.get(key) or "").strip()
        return Path(v).expanduser() if v else None
    return opt("llmModelsDir"), opt("comfyModelsDir"), opt("comfyLauncher")
