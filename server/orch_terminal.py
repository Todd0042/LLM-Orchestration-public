"""The orchestrator card's three terminals: Claude, AGY and a local agent, each in its own tmux session.

Why terminals: you see everything the agent does, including compaction, instead of a summary. Why
tmux: the agents keep running when the browser closes or this server restarts, and you can attach
from a real terminal (`tmux -S <socket> attach -t orch-<card>-claude`).

One card per repo. Rules the server enforces rather than asks for:
- **The baton:** only the holder's terminal takes typing; the others are watch-only. It moves only
  when the holder is idle (or you interrupt it), after the holder has updated `.orch/context.md`.
- **Branch-only work:** the agents' git uses this server's hooks (via GIT_CONFIG_* in their
  environment), which refuse commits and pushes anywhere but `orchestrator-modified` and then run
  the repo's own hooks. Your own terminals are unaffected. Merging to main is yours (the card's
  Merge button, or by hand).
- **Watch-only over the internet:** enforced in the routes and the WebSocket.

Busy/idle: Claude reports it through its UserPromptSubmit/Stop hooks; AGY and opencode count as
busy while their screen keeps changing. A usage-limit message on screen marks the agent out until
its reset time; the card offers a handoff but never makes one by itself.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import sys
import termios
import time
from typing import Any
import uuid

from .agent_chain import parse_reset, until_text

BRANCH = "orchestrator-modified"
SLOTS = ("claude", "agy", "local")
LABELS = {"claude": "Claude", "agy": "AGY", "local": "Local"}
AVAILABILITY_IDS = {"claude": "claude-cli", "agy": "agy-cli"}
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# Stricter than agent_chain.LIMIT_RE: a screen full of code can say "rate limit" or "429" without
# the agent being out of usage. These are the agents' own limit messages.
SCREEN_LIMIT_RE = re.compile(
    r"you'?ve hit your [\w -]*limit|usage limit reached|out of (?:extra )?usage|out of usage credits|"
    r"resource[_ ]exhausted|quota exceeded|exceeded your (?:current )?quota",
    re.IGNORECASE,
)
HOOK_NAMES = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit", "prepare-commit-msg",
              "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push", "post-rewrite")
GUARDED_HOOKS = ("pre-commit", "pre-merge-commit")

CONTEXT_TEMPLATE = """# Orchestrator context: {repo}

The shared notes of the Claude, AGY and local terminals working on this repo. Read it when you take
the baton; update it before you hand off. Work only on the `{branch}` branch; merging to main is the
user's decision. Keep one `###` section per task under each heading.

## Goal

## Branch
`{branch}`, started from `{base}` at {sha} on {date}.

## Done so far

## Decisions

## Next steps

## Handoff log
"""

TMUX_CONF = """# Written by LLM-Orchestration for the orchestrator terminals (its own tmux server, not yours).
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",xterm-256color:RGB"
set -g history-limit 50000
set -g remain-on-exit on
set -g status off
set -g mouse on
set -g escape-time 10
set -g window-size latest
set -g focus-events on
# No prefix key: Claude Code uses Ctrl-B itself. Close the window to detach.
set -g prefix None
unbind C-b
"""

HOOK_SCRIPT = """#!/usr/bin/env python3
# Claude Code hook -> LLM-Orchestration: reports busy/idle for one orchestrator terminal.
import json, os, sys, urllib.request
sys.stdin.read()
body = json.dumps({"card": os.environ["ORCH_CARD"], "slot": os.environ["ORCH_SLOT"], "event": sys.argv[1]}).encode()
req = urllib.request.Request(os.environ["ORCH_HOOK_URL"], data=body, method="POST", headers={
    "Content-Type": "application/json", "X-Orch-Client": "1", "X-Orch-Hook": os.environ["ORCH_HOOK_SECRET"]})
try:
    urllib.request.urlopen(req, timeout=3).read()
except Exception:
    pass
"""

GIT_HOOK = """#!/bin/sh
# LLM-Orchestration branch guard for the orchestrator's agent terminals (set through GIT_CONFIG_* in
# their environment only). Refuses work outside {branch}, then runs the repo's own hook.
hook=$(basename "$0")
case "$hook" in
  pre-commit|pre-merge-commit)
    b=$(git symbolic-ref --short -q HEAD)
    if [ "$b" != "{branch}" ]; then
      echo "orchestrator: agents commit only on {branch} (this is '${{b:-a detached HEAD}}'). Run: git switch {branch}" >&2
      exit 1
    fi ;;
  pre-push)
    input=$(cat)
    bad=$(printf '%s\\n' "$input" | awk 'NF && $3 != "refs/heads/{branch}" {{print $3}}')
    if [ -n "$bad" ]; then
      echo "orchestrator: agents push only {branch}, not: $bad" >&2
      exit 1
    fi ;;
esac
gd=$(git rev-parse --git-common-dir)
own=$(git config --file "$gd/config" --get core.hooksPath 2>/dev/null)
if [ -n "$own" ]; then
  case "$own" in /*) d="$own" ;; *) d="$(git rev-parse --show-toplevel)/$own" ;; esac
else
  d="$gd/hooks"
fi
[ -x "$d/$hook" ] || exit 0
if [ "$hook" = pre-push ]; then printf '%s\\n' "$input" | "$d/$hook" "$@"; exit $?; fi
exec "$d/$hook" "$@"
"""


class TerminalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise TerminalError(409, f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:400]}")
    return p.stdout.strip()


class Tmux:
    """This app's own tmux server (its own socket and config), started outside the service's cgroup."""

    def __init__(self, runtime_dir: Path, detach_with_systemd):
        self.sock = runtime_dir / "orch-tmux.sock"
        self.conf = runtime_dir / "orch-tmux.conf"
        self._detach = detach_with_systemd
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(TMUX_CONF, encoding="utf-8")

    # Targets: "=name" is a session (exact match); pane commands need "=name:" (its current pane).
    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-S", str(self.sock), *args], capture_output=True, text=True, timeout=15, check=check)

    def server_running(self) -> bool:
        return self.sock.exists() and self._run("list-sessions").returncode == 0

    def has(self, name: str) -> bool:
        return self._run("has-session", "-t", f"={name}").returncode == 0

    def start(self, name: str, cwd: str, env: dict[str, str], argv: list[str]) -> None:
        env_args = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
        if self.has(name):
            self._run("respawn-pane", "-k", "-t", f"={name}:", "-c", cwd, *env_args, *argv, check=True)
            return
        cmd = ["tmux", "-S", str(self.sock), "-f", str(self.conf), "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
               "-c", cwd, *env_args, *argv]
        if not self.server_running() and self._detach() and shutil.which("systemd-run"):
            # Own scope: restarting the llm-orchestration service must not kill the agents.
            cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit=llm-orch-tmux-{secrets.token_hex(3)}", *cmd]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            raise TerminalError(500, f"tmux could not start {name}: {(p.stderr or p.stdout).strip()[:300]}")

    def kill(self, name: str) -> None:
        self._run("kill-session", "-t", f"={name}")

    def pane(self, name: str) -> dict[str, Any] | None:
        # display-message "succeeds" with empty values for a missing session, so the name is checked too
        p = self._run("display-message", "-p", "-t", f"={name}:", "#{session_name}|#{pane_dead}|#{window_activity}|#{history_size}|#{pane_pid}")
        session, dead, activity, hist, pid = (p.stdout.strip().split("|") + [""] * 5)[:5]
        if p.returncode != 0 or session != name:
            return None
        return {"dead": dead == "1", "activity": float(activity or 0), "history": int(hist or 0), "pid": int(pid or 0)}

    def capture(self, name: str, lines: int = 20, join: bool = True) -> list[str]:
        p = self._run("capture-pane", "-p", *(["-J"] if join else []), "-S", f"-{lines}", "-t", f"={name}:")
        return p.stdout.splitlines() if p.returncode == 0 else []

    def type_line(self, name: str, text: str) -> None:
        self._run("send-keys", "-t", f"={name}:", "-l", text, check=True)
        self._run("send-keys", "-t", f"={name}:", "Enter", check=True)

    def keys(self, name: str, *keys: str) -> None:
        self._run("send-keys", "-t", f"={name}:", *keys, check=True)

    def attach_argv(self, name: str, read_only: bool) -> list[str]:
        return ["tmux", "-S", str(self.sock), "attach-session", *(["-r"] if read_only else []), "-t", f"={name}"]


class OrchTerminals:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.runtime = ctx.data_dir / "runtime"
        self.tmux = Tmux(self.runtime, lambda: bool(self._cfg().get("detachWithSystemd", True)))
        self._hook_busy: dict[tuple[str, str], bool] = {}
        self._hook_seen: set[tuple[str, str]] = set()
        self._stops: dict[tuple[str, str], int] = {}
        self._out: dict[tuple[str, str], tuple[float, str]] = {}
        self._limit_seen: dict[tuple[str, str], set[tuple[int, str]]] = {}
        self._screens: dict[tuple[str, str], tuple[int, float]] = {}  # screen text hash, when it last changed
        self._handoffs: dict[str, dict[str, Any]] = {}
        self._opencode_flags: dict[str, bool] = {}
        self._write_helpers()

    # ---- setup ---------------------------------------------------------------------------------
    def _cfg(self) -> dict[str, Any]:
        return self.ctx.settings().get("orchTerminal") or {}

    def _write_helpers(self) -> None:
        hooks = self.runtime / "orch-git-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        body = GIT_HOOK.format(branch=BRANCH)
        for name in HOOK_NAMES:
            f = hooks / name
            f.write_text(body, encoding="utf-8")
            f.chmod(0o755)
        script = self.runtime / "orch-claude-hook.py"
        script.write_text(HOOK_SCRIPT, encoding="utf-8")
        script.chmod(0o700)
        claude_hooks = {"hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": f"{sys.executable} {script} idle"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{sys.executable} {script} busy"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": f"{sys.executable} {script} idle"}]}],
        }}
        (self.runtime / "orch-claude-settings.json").write_text(json.dumps(claude_hooks, indent=2), encoding="utf-8")

    def _session(self, cid: str, slot: str) -> str:
        return f"orch-{cid}-{slot}"

    # ---- cards ---------------------------------------------------------------------------------
    def _row(self, cid: str):
        row = self.ctx.conn.execute("SELECT * FROM orch_cards WHERE id=?", (cid,)).fetchone()
        if not row:
            raise TerminalError(404, "No such orchestrator card")
        return row

    def _repo_path(self, repo: str) -> Path:
        if not REPO_NAME_RE.match(repo or ""):
            raise TerminalError(400, "Give the repo's folder name in your GitHub folder")
        root = self.ctx.github_dir().resolve()
        path = (root / repo).resolve()
        if path.parent != root or not (path / ".git").exists():
            raise TerminalError(404, f"No git repo named {repo} in {root}")
        return path

    def list_cards(self) -> list[dict[str, Any]]:
        rows = self.ctx.conn.execute("SELECT * FROM orch_cards ORDER BY created_at").fetchall()
        return [self.describe(r) for r in rows]

    def describe(self, row) -> dict[str, Any]:
        cid = row["id"]
        slots = {s: self.slot_status(cid, s) for s in SLOTS}
        return {"id": cid, "repo": row["repo"], "workdir": row["workdir"], "branch": BRANCH, "holder": row["holder"],
                "slots": slots, "handoff": self._handoffs.get(cid), "labels": LABELS,
                "attachCommand": " ".join(self.tmux.attach_argv(self._session(cid, row["holder"]), False))}

    def slot_status(self, cid: str, slot: str) -> dict[str, Any]:
        key = (cid, slot)
        name = self._session(cid, slot)
        pane = self.tmux.pane(name)
        state = "stopped" if pane is None else "exited" if pane["dead"] else "running"
        screen = self.tmux.capture(name, 40) if pane else []
        self._note_screen(key, screen)
        until, reason = self._out.get(key, (0.0, ""))
        if until and until <= time.time():
            self._out.pop(key, None)
            until, reason = 0.0, ""
        return {"state": state, "busy": state == "running" and self._busy(key), "outUntil": until,
                "outText": until_text(until) if until else "", "outReason": reason,
                "lastLine": next((ln.strip() for ln in reversed(screen) if ln.strip()), "")}

    def _note_screen(self, key: tuple[str, str], screen: list[str]) -> None:
        """Remember when the screen's text last changed. Only the newest 15 non-blank lines count, captured
        joined (-J): a client attaching at another size rewraps lines and moves the visible area, but
        doesn't change those."""
        digest = hash(tuple([ln.rstrip() for ln in screen if ln.strip()][-15:]))
        old = self._screens.get(key)
        if old is None or old[0] != digest:
            self._screens[key] = (digest, time.time() if old is not None else 0.0)

    def _busy(self, key: tuple[str, str]) -> bool:
        changed = self._screens.get(key, (0, 0.0))[1]
        if key[1] == "claude" and key in self._hook_seen:
            # Claude Code fires no Stop hook when it's interrupted or fails, so "busy" from the hooks
            # also needs a moving screen: its spinner and timer redraw every second while it works.
            stale = float(self._cfg().get("claudeStaleBusySec", 15))
            return self._hook_busy.get(key, False) and time.time() - changed < stale
        quiet = float(self._cfg().get("idleAfterSec", 3))
        return time.time() - changed < quiet

    async def open_card(self, repo: str) -> dict[str, Any]:
        path = self._repo_path(repo)
        row = self.ctx.conn.execute("SELECT * FROM orch_cards WHERE repo=?", (repo,)).fetchone()
        if row is None:
            await asyncio.to_thread(self._prepare_branch, path)
            cid = uuid.uuid4().hex[:8]
            now = time.time()
            self.ctx.conn.execute(
                "INSERT INTO orch_cards (id, repo, workdir, holder, claude_session, hook_secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, repo, str(path), "claude", str(uuid.uuid4()), secrets.token_urlsafe(24), now, now))
            self.ctx.conn.commit()
            row = self._row(cid)
            self.ctx.audit.record("orch_card_open", repo, card=cid)
        if self.slot_status(row["id"], row["holder"])["state"] == "stopped":
            await self.start_slot(row["id"], row["holder"])
        return self.describe(self._row(row["id"]))

    def _prepare_branch(self, path: Path) -> None:
        current = _git(path, "branch", "--show-current", check=False)
        if current != BRANCH:
            if _git(path, "status", "--porcelain"):
                raise TerminalError(409, f"{path.name} has uncommitted changes on {current or 'a detached HEAD'}. "
                                         "Commit or stash them before the orchestrator switches it to its own branch.")
            if _git(path, "rev-parse", "--verify", "-q", f"refs/heads/{BRANCH}", check=False):
                _git(path, "checkout", "-q", BRANCH)
            else:
                _git(path, "fetch", "-q", "origin", check=False)
                if _git(path, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{BRANCH}", check=False):
                    _git(path, "checkout", "-q", "-b", BRANCH, "--track", f"origin/{BRANCH}")
                else:
                    _git(path, "checkout", "-q", "-b", BRANCH, self._base_branch(path))
        ctx_file = path / ".orch" / "context.md"
        if not ctx_file.exists():
            self._write_template(path)

    def _base_branch(self, path: Path) -> str:
        head = _git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
        if head.startswith("origin/"):
            return head.split("/", 1)[1]
        for b in ("main", "master"):
            if _git(path, "rev-parse", "--verify", "-q", f"refs/heads/{b}", check=False):
                return b
        return _git(path, "branch", "--show-current")

    def _write_template(self, path: Path) -> None:
        base = self._base_branch(path)
        (path / ".orch").mkdir(exist_ok=True)
        (path / ".orch" / "context.md").write_text(CONTEXT_TEMPLATE.format(
            repo=path.name, branch=BRANCH, base=base, sha=_git(path, "rev-parse", "--short", base),
            date=dt.date.today().isoformat()), encoding="utf-8")

    # ---- starting agents -------------------------------------------------------------------------
    def _argv(self, row, slot: str, resume: bool) -> list[str]:
        cfg = self._cfg()
        cmds = {"claude": "claude", "agy": "agy", "local": "opencode", **(cfg.get("commands") or {})}
        exe = shutil.which(cmds[slot]) or cmds[slot]
        if slot == "claude":
            argv = [exe, "--dangerously-skip-permissions", "--remote-control", f"{self.ctx.nodes.local_id}-{row['repo']}"]
            argv += ["--resume", row["claude_session"]] if resume else ["--session-id", row["claude_session"]]
            argv += ["--settings", str(self.runtime / "orch-claude-settings.json"),
                     "--mcp-config", self.ctx.agents.mcp_config_path(),
                     "--append-system-prompt-file", str(self.runtime / f"orch-briefing-{row['id']}.md")]
            return argv
        if slot == "agy":
            return [exe, "--dangerously-skip-permissions", *(["--continue"] if resume else [])]
        # The model comes from this card's OPENCODE_CONFIG. opencode 2.x needs --standalone to use it
        # (it otherwise joins a shared background service); 1.x has no such flag.
        standalone = ["--standalone"] if self._opencode_has_standalone(exe) else []
        return [exe, *standalone, "--auto", *(["-c"] if resume else []), row["workdir"]]

    def _opencode_has_standalone(self, exe: str) -> bool:
        if exe not in self._opencode_flags:
            try:
                p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
                self._opencode_flags[exe] = "--standalone" in (p.stdout + p.stderr)
            except Exception:
                self._opencode_flags[exe] = False
        return self._opencode_flags[exe]

    def _local_model(self) -> str:
        s = self.ctx.settings()
        model = str(self._cfg().get("localModel") or "")
        if not model:
            code = str((s.get("taskDefaults") or {}).get("coding", {}).get("modelId") or "")
            model = code if code and not code.endswith("-cli") else str(s["engines"]["orchestrator"].get("modelId") or "")
        return model

    def _opencode_config(self) -> Path:
        s = self.ctx.settings()
        model = self._local_model()
        cfg = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
               "provider": {"local": {"npm": "@ai-sdk/openai-compatible", "name": "Local models",
                                      "options": {"baseURL": str(s["paths"].get("llmBaseUrl") or "")},
                                      "models": {model: {"name": model, "tool_call": True}}}},  # without tool_call opencode offers it no tools
               "model": f"local/{model}",
               "mcp": {"llm-orchestration": {"type": "local", "enabled": True,
                                             "command": [sys.executable, str(Path(__file__).with_name("mcp_bridge.py"))],
                                             "environment": {"LLM_ORCH_URL": f"http://127.0.0.1:{self._port()}"}}}}
        f = self.runtime / "orch-opencode.json"
        f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return f

    def _port(self) -> int:
        return int(self.ctx.settings()["node"].get("port", 8790))

    def _env(self, row, slot: str) -> dict[str, str]:
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(self.runtime / "orch-git-hooks"),
               "ORCH_CARD": row["id"], "ORCH_SLOT": slot, "ORCH_HOOK_SECRET": row["hook_secret"],
               "ORCH_HOOK_URL": f"http://127.0.0.1:{self._port()}/api/orch-terminal/hook", "TERM": "xterm-256color",
               "COLORTERM": "truecolor"}
        if slot == "local":
            env["OPENCODE_CONFIG"] = str(self._opencode_config())
        return env

    async def _write_briefing(self, row) -> None:
        base = await self.ctx.orchestrator.briefing()
        extra = "\n".join([
            "",
            "THE ORCHESTRATOR CARD:",
            f"- You are the Claude terminal of the orchestrator card for `{row['repo']}` ({row['workdir']}). AGY and a local agent "
            "share this card; only one works at a time, and the user moves the baton between you.",
            f"- Work only on the `{BRANCH}` branch. Your git refuses commits and pushes anywhere else; never try to get around "
            "that (no --no-verify). Merging to main is the user's decision.",
            "- `.orch/context.md` is the shared memory of this card: read it when you take over, keep it current, and update it "
            "when asked to before a handoff.",
        ])
        (self.runtime / f"orch-briefing-{row['id']}.md").write_text(base + "\n" + extra + "\n", encoding="utf-8")

    async def start_slot(self, cid: str, slot: str, resume: bool = False, fresh: bool = False) -> dict[str, Any]:
        if slot not in SLOTS:
            raise TerminalError(404, "No such terminal")
        row = self._row(cid)
        if fresh and slot == "claude":
            self.ctx.conn.execute("UPDATE orch_cards SET claude_session=?, updated_at=? WHERE id=?", (str(uuid.uuid4()), time.time(), cid))
            self.ctx.conn.commit()
            row = self._row(cid)
        if slot == "claude":
            await self._write_briefing(row)
        status = self.slot_status(cid, slot)
        if status["state"] == "running":
            return status
        # A Claude session id can only be started once; after that it has to be resumed.
        if slot == "claude" and not fresh and status["state"] == "exited":
            resume = True
        key = (cid, slot)
        self._hook_seen.discard(key)
        self._hook_busy.pop(key, None)
        await asyncio.to_thread(self.tmux.start, self._session(cid, slot), row["workdir"], self._env(row, slot), self._argv(row, slot, resume))
        self._limit_seen[key] = self._limit_lines(cid, slot)  # a resumed session's old limit message isn't news
        self.ctx.audit.record("orch_terminal_start", row["repo"], card=cid, slot=slot, resume=resume)
        await self._changed(cid)
        return self.slot_status(cid, slot)

    def open_alacritty(self, cid: str, slot: str) -> None:
        """The same live session in a real terminal window on this machine."""
        if slot not in SLOTS or not self.tmux.has(self._session(cid, slot)):
            raise TerminalError(404, "That terminal isn't running")
        exe = shutil.which("alacritty")
        if not exe:
            raise TerminalError(404, "Alacritty isn't installed on this machine")
        subprocess.Popen([exe, "--title", f"{self._row(cid)['repo']} · {LABELS[slot]}", "-e", *self.tmux.attach_argv(self._session(cid, slot), False)],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_card(self, cid: str) -> None:
        row = self._row(cid)
        for s in SLOTS:
            self.tmux.kill(self._session(cid, s))
        self.ctx.conn.execute("DELETE FROM orch_cards WHERE id=?", (cid,))
        self.ctx.conn.commit()
        self.ctx.audit.record("orch_card_close", row["repo"], card=cid)

    # ---- input -----------------------------------------------------------------------------------
    def can_type(self, cid: str, slot: str, zone: str) -> bool:
        return zone != "tunnel" and self._row(cid)["holder"] == slot and cid not in self._handoffs

    def send(self, cid: str, text: str) -> None:
        row = self._row(cid)
        if cid in self._handoffs:
            raise TerminalError(409, "A handoff is in progress")
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if not text:
            raise TerminalError(400, "Nothing to send")
        self.tmux.type_line(self._session(cid, row["holder"]), text)

    def interrupt(self, cid: str) -> None:
        row = self._row(cid)
        keys = (self._cfg().get("interruptKeys") or {}).get(row["holder"]) or ["Escape"]
        self.tmux.keys(self._session(cid, row["holder"]), *keys)
        self._hook_busy[(cid, row["holder"])] = False  # an interrupted Claude fires no Stop hook
        self.ctx.audit.record("orch_terminal_interrupt", row["repo"], card=cid, slot=row["holder"])

    # ---- busy/idle and usage ----------------------------------------------------------------------
    def hook(self, cid: str, slot: str, event: str, secret: str) -> bool:
        row = self.ctx.conn.execute("SELECT hook_secret FROM orch_cards WHERE id=?", (cid,)).fetchone()
        if not row or slot not in SLOTS or not hmac.compare_digest(str(row["hook_secret"]), secret or ""):
            return False
        key = (cid, slot)
        self._hook_seen.add(key)
        self._hook_busy[key] = event == "busy"
        if event == "idle":
            self._stops[key] = self._stops.get(key, 0) + 1
        asyncio.get_running_loop().create_task(self._changed(cid))
        return True

    def _limit_lines(self, cid: str, slot: str) -> set[tuple[int, str]]:
        """Usage-limit lines on screen, keyed by absolute line number so a redraw isn't a new message."""
        name = self._session(cid, slot)
        pane = self.tmux.pane(name)
        if not pane:
            return set()
        first = max(0, pane["history"] - 40)
        return {(first + i, ln.strip()) for i, ln in enumerate(self.tmux.capture(name, 40, join=False)) if SCREEN_LIMIT_RE.search(ln)}

    async def watch(self) -> None:
        """One pass: mark an agent out of usage when a new limit message appears on its screen."""
        rows = self.ctx.conn.execute("SELECT id FROM orch_cards").fetchall()
        if not rows or not self.tmux.server_running():
            return
        for row in rows:
            cid = row["id"]
            for slot in SLOTS:
                key = (cid, slot)
                await asyncio.to_thread(self.slot_status, cid, slot)  # keeps busy/idle current between requests
                found = await asyncio.to_thread(self._limit_lines, cid, slot)
                if key not in self._limit_seen:  # first look (e.g. after a restart): old messages aren't news
                    self._limit_seen[key] = found
                    continue
                for _, line in sorted(found - self._limit_seen[key]):
                    until = parse_reset(line) or (time.time() + 30 * 60)
                    self._out[key] = (until, line[:200])
                    if slot in AVAILABILITY_IDS:
                        self.ctx.availability.mark_exhausted(AVAILABILITY_IDS[slot], line)
                    self.ctx.audit.record("orch_terminal_out_of_usage", slot, card=cid, until=until)
                    await self._changed(cid)
                self._limit_seen[key] = found

    async def _changed(self, cid: str) -> None:
        try:
            await self.ctx.emit({"type": "orch_card", "cardId": cid})
        except Exception:
            pass

    # ---- handoff ---------------------------------------------------------------------------------
    def start_handoff(self, cid: str, to: str) -> None:
        row = self._row(cid)
        frm = row["holder"]
        if to not in SLOTS:
            raise TerminalError(404, "No such terminal")
        if to == frm:
            raise TerminalError(400, f"{LABELS[to]} already holds the baton")
        if cid in self._handoffs:
            raise TerminalError(409, "A handoff is already in progress")
        st = self.slot_status(cid, frm)
        if st["busy"]:
            raise TerminalError(409, f"{LABELS[frm]} is still working. Let it finish, or interrupt it first.")
        self._handoffs[cid] = {"from": frm, "to": to, "phase": "starting", "error": ""}
        asyncio.get_running_loop().create_task(self._handoff(cid, frm, to, st))

    async def _handoff(self, cid: str, frm: str, to: str, frm_status: dict[str, Any]) -> None:
        h = self._handoffs[cid]
        row = self._row(cid)
        workdir = Path(row["workdir"])
        try:
            reason = ""
            if frm_status["outUntil"]:
                reason = f" ({LABELS[frm]} out of usage until {frm_status['outText']}; it couldn't update the notes)"
            elif frm_status["state"] != "running":
                reason = f" ({LABELS[frm]} wasn't running; it couldn't update the notes)"
            else:
                h["phase"] = "updating"
                await self._changed(cid)
                if not await self._ask_update(cid, frm, to):
                    raise TerminalError(504, f"{LABELS[frm]} didn't finish updating the notes in time; the baton stays with it")
            h["phase"] = "passing"
            ctx_file = workdir / ".orch" / "context.md"
            if not ctx_file.exists():
                await asyncio.to_thread(self._write_template, workdir)
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            with ctx_file.open("a", encoding="utf-8") as fh:
                fh.write(f"- {stamp} {LABELS[frm]} → {LABELS[to]} on {self.ctx.nodes.local_id}{reason}\n")
            self._backup_context(row["repo"], ctx_file)
            self._out.pop((cid, to), None)
            to_state = self.slot_status(cid, to)["state"]
            if to_state != "running":
                await self.start_slot(cid, to, resume=to_state == "exited")
                await self._wait_quiet(cid, to, 20)
            self.ctx.conn.execute("UPDATE orch_cards SET holder=?, updated_at=? WHERE id=?", (to, time.time(), cid))
            self.ctx.conn.commit()
            await asyncio.to_thread(self.tmux.type_line, self._session(cid, to), (
                f"Taking over from {LABELS[frm]}. Read .orch/context.md, then check the branch: run git fetch and git log on "
                f"{BRANCH} to see what changed since you last held the baton, and tell me what changed. Work only on {BRANCH}. "
                "Then wait for instructions."))
            self.ctx.audit.record("orch_handoff", row["repo"], card=cid, frm=frm, to=to)
            self._handoffs.pop(cid, None)
        except Exception as exc:  # the baton stays where it was; the card shows why
            h.update({"phase": "failed", "error": str(exc)[:300]})
            self.ctx.audit.record("orch_handoff_failed", row["repo"], card=cid, frm=frm, to=to, error=str(exc)[:300])
            await self._changed(cid)
            await asyncio.sleep(30)
            if self._handoffs.get(cid) is h:
                self._handoffs.pop(cid, None)
        await self._changed(cid)

    async def _ask_update(self, cid: str, frm: str, to: str) -> bool:
        key = (cid, frm)
        stops = self._stops.get(key, 0)
        await asyncio.to_thread(self.tmux.type_line, self._session(cid, frm), (
            f"Update .orch/context.md now: record what you did under Done so far, any Decisions, and the Next steps. "
            f"Then stop: you are handing the baton to {LABELS[to]}."))
        timeout = float(self._cfg().get("handoffUpdateTimeoutSec", 900))
        deadline = time.time() + timeout
        if frm == "claude" and key in self._hook_seen:
            while time.time() < deadline:
                if self._stops.get(key, 0) > stops:
                    return True
                await asyncio.sleep(0.5)
            return False
        await asyncio.sleep(1.5)
        return await self._wait_quiet(cid, frm, timeout)

    async def _wait_quiet(self, cid: str, slot: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.slot_status(cid, slot)["busy"]:
                return True
            await asyncio.sleep(0.5)
        return False

    def _vault_dir(self, repo: str) -> Path:
        d = self.ctx.vault_dir() / "orchestrator" / repo
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _backup_context(self, repo: str, ctx_file: Path) -> None:
        dest = self._vault_dir(repo) / "context.md"
        tmp = dest.with_suffix(".tmp")
        shutil.copyfile(ctx_file, tmp)
        tmp.chmod(0o444)  # read-only: agents use the file in the repo, never this copy
        tmp.replace(dest)

    # ---- merge ---------------------------------------------------------------------------------
    async def merge(self, cid: str) -> dict[str, Any]:
        row = self._row(cid)
        if cid in self._handoffs:
            raise TerminalError(409, "A handoff is in progress")
        busy = [LABELS[s] for s in SLOTS if self.slot_status(cid, s)["busy"]]
        if busy:
            raise TerminalError(409, f"{', '.join(busy)} still working; merge when they're idle")
        result = await asyncio.to_thread(self._merge, row)
        self.ctx.audit.record("orch_merge", row["repo"], card=cid, **result)
        await self._changed(cid)
        return result

    def _merge(self, row) -> dict[str, Any]:
        path = Path(row["workdir"])
        base = self._base_branch(path)
        if _git(path, "branch", "--show-current", check=False) != BRANCH:
            raise TerminalError(409, f"{path.name} isn't on {BRANCH}; switch back before merging")
        ctx_file = path / ".orch" / "context.md"
        archived = ""
        if ctx_file.exists():
            dest = self._vault_dir(row["repo"]) / "archive"
            dest.mkdir(exist_ok=True)
            target = dest / f"{dt.datetime.now().strftime('%Y-%m-%d-%H%M%S')}-context.md"
            shutil.copyfile(ctx_file, target)
            archived = str(target)
        tracked_ctx = bool(_git(path, "ls-files", ".orch", check=False))
        if _git(path, "status", "--porcelain", "--untracked-files=no", "--", ".", ":!.orch"):
            raise TerminalError(409, f"{BRANCH} has uncommitted changes; have the agent commit them first")
        if not tracked_ctx and ctx_file.exists():
            ctx_file.unlink()  # archived above; the fresh branch gets a new one
        elif tracked_ctx:
            _git(path, "checkout", "-q", "--", ".orch")
        ahead = int(_git(path, "rev-list", "--count", f"{base}..{BRANCH}") or 0)
        _git(path, "checkout", "-q", base)
        try:
            if ahead:
                _git(path, "merge", "--no-ff", "--no-commit", BRANCH)
                _git(path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", ".orch")
                if (path / ".orch").exists():
                    shutil.rmtree(path / ".orch")
                _git(path, "commit", "-q", "-m", f"Merge {BRANCH}")
        except TerminalError:
            _git(path, "merge", "--abort", check=False)
            _git(path, "checkout", "-q", BRANCH, check=False)
            raise
        pushed = False
        if _git(path, "remote", check=False):
            p = subprocess.run(["git", "-C", str(path), "push", "-q", "origin", base], capture_output=True, text=True, timeout=120)
            pushed = p.returncode == 0
            subprocess.run(["git", "-C", str(path), "push", "-q", "origin", "--delete", BRANCH], capture_output=True, text=True, timeout=60)
        _git(path, "branch", "-D", BRANCH)
        _git(path, "checkout", "-q", "-b", BRANCH, base)
        self._write_template(path)
        return {"merged": ahead, "base": base, "pushed": pushed, "archived": archived}

    # ---- GitHub branch protection ------------------------------------------------------------------
    async def protect(self, cid: str) -> dict[str, Any]:
        row = self._row(cid)
        path = Path(row["workdir"])
        url = await asyncio.to_thread(_git, path, "remote", "get-url", "origin", False)
        m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
        if not m:
            raise TerminalError(400, "This repo has no GitHub origin")
        owner, name = m.group(1), m.group(2)
        base = self._base_branch(path)
        payload = {"required_status_checks": None, "enforce_admins": False, "required_pull_request_reviews": None,
                   "restrictions": None, "allow_force_pushes": False, "allow_deletions": False}
        p = await asyncio.to_thread(subprocess.run, ["gh", "api", "-X", "PUT", f"repos/{owner}/{name}/branches/{base}/protection",
                                                     "--input", "-"], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0:
            if "Upgrade to GitHub Pro" in out:
                raise TerminalError(402, f"GitHub only protects branches of private repos on paid plans. {owner}/{name} is private, "
                                         "so main can't be protected; the agents' git hooks still refuse commits on main.")
            raise TerminalError(502, f"GitHub refused: {out[:300]}")
        self.ctx.audit.record("orch_protect_main", f"{owner}/{name}", branch=base)
        return {"repo": f"{owner}/{name}", "branch": base, "protected": True, "adminsBypass": True}

    def context_text(self, cid: str) -> str:
        f = Path(self._row(cid)["workdir"]) / ".orch" / "context.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""


# ---- browser terminal: a PTY running `tmux attach` -----------------------------------------------

def _controlling_tty() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class AttachedTerminal:
    """One browser's view of one tmux session, through its own `tmux attach` in a pseudo-terminal."""

    def __init__(self, argv: list[str], cols: int = 120, rows: int = 32):
        self.argv = argv
        self.master = -1
        self.proc: asyncio.subprocess.Process | None = None
        self.cols, self.rows = cols, rows

    async def start(self, on_output) -> None:
        import pty

        self.master, slave = pty.openpty()
        self.resize(self.cols, self.rows)
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        env.pop("TMUX", None)
        self.proc = await asyncio.create_subprocess_exec(*self.argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                                         preexec_fn=_controlling_tty)
        os.close(slave)
        fl = fcntl.fcntl(self.master, fcntl.F_GETFL)
        fcntl.fcntl(self.master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        loop = asyncio.get_running_loop()

        def readable() -> None:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(self.master)
                on_output(None)
                return
            on_output(data)

        loop.add_reader(self.master, readable)

    def write(self, data: bytes) -> None:
        if self.master >= 0:
            os.write(self.master, data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(20, min(cols, 500)), max(5, min(rows, 200))
        if self.master >= 0:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))

    async def close(self) -> None:
        if self.master >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except Exception:
                pass
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()  # detaches this client only; the agent keeps running
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self.master >= 0:
            os.close(self.master)
            self.master = -1
