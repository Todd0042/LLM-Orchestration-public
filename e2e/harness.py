"""Two-node E2E harness: real server processes, fake model server, fake CLIs, real git repos.

Nothing here touches your real machines: each node gets its own temporary HOME, config,
data, GitHub dir and vault; `systemctl`, `docker`, `agy`, `claude` are fakes on PATH that
record what they were asked to do.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TOKEN = "e2e-shared-node-token-0123456789abcdef0123456789"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
failures: list[str] = []


def check(cond: bool, label: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}", flush=True)
    if not cond:
        failures.append(label)
    return cond


def free_port_block(n: int = 3) -> int:
    """A base port with base..base+n-1 free (main, tunnel, maintenance)."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        if base + n > 65000:
            continue
        ok = True
        for i in range(1, n):
            with socket.socket() as t:
                try:
                    t.bind(("127.0.0.1", base + i))
                except OSError:
                    ok = False
        if ok:
            return base
    raise RuntimeError("no free port block")


def wait_until(fn, timeout: float = 20.0, step: float = 0.25, label: str = "") -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - polling
            last = exc
        time.sleep(step)
    return None


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com", "GIT_COMMITTER_NAME": "E2E",
           "GIT_COMMITTER_EMAIL": "e2e@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


# ---- fake OpenAI-compatible model server ----------------------------------------------------------

class FakeLLM:
    """Scripted model: 'RUN:<cmd>' in the user turn -> orch_run_command tool call; after a tool
    result -> a final answer quoting it; otherwise echoes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, obj: Any, code: int = 200) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/models"):
                    return self._send({"data": [{"id": "fake-model"}, {"id": "qwen3-8b"}]})
                self._send({"detail": "nope"}, 404)

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append(body)
                msgs = body.get("messages") or []
                last = msgs[-1] if msgs else {}
                first = str((msgs[0] if msgs else {}).get("content") or "")
                if first.startswith("You route requests"):
                    req = str(last.get("content") or "").split("Request:", 1)[-1].lower()
                    kind = "ops" if "run:" in req else "story" if "story" in req else "code" if any(w in req for w in ("code", "bug", "repo", "fix", "review")) else "chat"
                    return self._send({"choices": [{"message": {"role": "assistant", "content": json.dumps({"kind": kind, "repo": ""})}}]})
                if last.get("role") == "tool":
                    return self._send({"choices": [{"message": {"role": "assistant", "content": f"Tool result: {str(last.get('content'))[:300]}"}}]})
                text = str(last.get("content") or "")
                if "RUN:" in text and body.get("tools"):
                    cmd = text.split("RUN:", 1)[1].strip()
                    return self._send({"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "orch_run_command", "arguments": json.dumps({"command": cmd, "targetNode": "local"})}}]}}]})
                self._send({"choices": [{"message": {"role": "assistant", "content": f"echo: {text[:200]}"}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class StatusServer:
    """Tiny HTTP endpoint whose status code the test controls (for watchdog/failover)."""

    def __init__(self, code: int = 200) -> None:
        self.code = code
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(outer.code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/health"


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
# Like the real CLI: --mcp-config takes one or more values, so a prompt placed after it is
# swallowed as a config path. The prompt must come right after -p.
if "--mcp-config" in args:
    i = args.index("--mcp-config") + 1
    while i < len(args) and not args[i].startswith("-"):
        if not os.path.isfile(args[i]):
            print(f"Error: Invalid MCP configuration:\nMCP config file not found: {args[i]}", file=sys.stderr)
            sys.exit(1)
        i += 1
prompt = args[args.index("-p") + 1] if "-p" in args and args.index("-p") + 1 < len(args) else ""
if os.path.exists(os.path.join(os.environ["FAKE_STATE"], "claude.limit")):
    with open(os.path.join(os.environ["FAKE_STATE"], "claude.log"), "a") as fh:
        fh.write(json.dumps({"cwd": os.getcwd(), "prompt": prompt, "argv": args, "limited": True}) + "\n")
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|4102444800"}))
    sys.exit(1)
session = args[args.index("--resume") + 1] if "--resume" in args else "fake-session-1"
if "WRITE:" in prompt:
    name, _, content = prompt.split("WRITE:", 1)[1].strip().partition(" ")
    open(os.path.join(os.getcwd(), name), "w").write(content)
with open(os.path.join(os.environ["FAKE_STATE"], "claude.log"), "a") as fh:
    fh.write(json.dumps({"cwd": os.getcwd(), "prompt": prompt, "argv": args}) + "\n")
print(json.dumps({"type": "system", "session_id": session, "model": "fake-claude"}))
print(json.dumps({"type": "assistant", "message": {"model": "fake-claude", "content": [{"type": "text", "text": f"claude ran in {os.getcwd()}"}]}}))
print(json.dumps({"type": "result", "result": f"claude ran in {os.getcwd()}", "session_id": session}))
'''

FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = next((a[3:] for a in args if a.startswith("-p=")), "")
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "agy.log"), "a") as fh:
    fh.write(json.dumps({"cwd": os.getcwd(), "prompt": prompt, "argv": args}) + "\n")
if os.path.exists(os.path.join(state, "agy.limit")):
    print(json.dumps({"event": "result", "result": {"error": "RESOURCE_EXHAUSTED: quota exceeded for this model"}}))
    sys.exit(1)
print(json.dumps({"event": "init", "conversation_id": "agy-conv-1"}))
print(json.dumps({"event": "step_update", "step_update": {"text_delta": f"agy ran in {os.getcwd()}"}}))
print(json.dumps({"event": "result", "result": {"response": f"agy ran in {os.getcwd()}", "conversation_id": "agy-conv-1"}}))
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
# fake `systemctl --user ...`: state per unit lives in $FAKE_STATE/units/<unit>
st="$FAKE_STATE"; mkdir -p "$st/units"
[ "$1" = "--user" ] && shift
cmd="$1"; shift
echo "$cmd $*" >> "$st/systemctl.log"
case "$cmd" in
  is-active) for u in "$@"; do s="$(cat "$st/units/$u" 2>/dev/null || echo active)"; echo "$s"; [ "$s" = active ] || exit 3; done ;;
  start|restart) for u in "$@"; do [ -f "$st/units/$u.sticky" ] || echo active > "$st/units/$u"; done ;;
  stop) for u in "$@"; do echo inactive > "$st/units/$u"; done ;;
esac
exit 0
'''

FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_STATE/docker.log"
[ "$1" = inspect ] && { echo "running healthy"; exit 0; }
exit 0
'''


class Node:
    def __init__(self, name: str, role: str, root: Path, llm: FakeLLM, hub_port_ref: dict[str, int]):
        self.name = name
        self.role = role
        self.root = root / name
        self.home = self.root / "home"
        self.cfg = self.root / "cfg"
        self.data = self.root / "data"
        self.github = self.home / ("Documents/GitHub" if name == "desktop" else "GitHub")
        self.vault = self.home / "LLM-Vault"
        self.state = self.root / "fake-state"
        self.bin = self.home / ".local" / "bin"
        for d in (self.home, self.cfg, self.data, self.github, self.vault, self.state, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        self.port = free_port_block()
        self.llm = llm
        self.proc: subprocess.Popen | None = None
        self.log = self.root / "server.log"
        (self.cfg / "node_token").write_text(TOKEN + "\n")
        os.chmod(self.cfg / "node_token", 0o600)
        (self.home / ".local/share/llm-orchestration-totp").mkdir(parents=True, exist_ok=True)
        (self.home / ".local/share/llm-orchestration-totp/totp_secret").write_text(TOTP_SECRET)
        for name_, body in (("claude", FAKE_CLAUDE), ("agy", FAKE_AGY), ("systemctl", FAKE_SYSTEMCTL), ("docker", FAKE_DOCKER)):
            f = self.bin / name_
            f.write_text(body)
            f.chmod(0o755)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def tunnel(self) -> str:
        return f"http://127.0.0.1:{self.port + 1}"

    def settings(self, peer: "Node", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        from server.config import default_settings

        s = default_settings()
        s["node"].update({"id": self.name, "displayName": self.name.title(), "role": self.role, "port": self.port,
                          "tunnelPort": self.port + 1, "bindHost": "127.0.0.1", "mdnsEnabled": False})
        s["peers"] = [{"id": peer.name, "name": peer.name.title(), "url": peer.url, "role": peer.role, "enabled": True}]
        s["paths"].update({"agyDir": str(self.home / ".gemini/antigravity-cli"), "claudeDir": str(self.home / ".claude"),
                           "toolsDir": str(ROOT / "tools"), "githubDir": str(self.github), "vaultDir": str(self.vault),
                           "llmBaseUrl": self.llm.base, "useGpuScheduler": False, "backupsDir": str(self.home / "Backups")})
        s["auth"]["totpStateDir"] = str(self.home / ".local/share/llm-orchestration-totp")
        s["engines"]["orchestrator"].update({"nodeId": "auto", "modelId": "fake-model"})
        for t in s["taskDefaults"].values():
            t["modelId"] = "fake-model" if t["modelId"] not in ("agy-cli",) else t["modelId"]
        s["replication"] = {"intervalSec": 1}
        s["monitor"] = {"intervalSec": 1, "failThreshold": 3, "maxRestartsPerHour": 2, "backoffBaseSec": 1, "backoffMaxSec": 2}
        s["alerts"] = {"ntfyUrl": "", "minIntervalSec": 0}
        s["terminal"]["approvalTimeoutSec"] = 20
        for k, v in (extra or {}).items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
        return s

    def write_settings(self, peer: "Node", extra: dict[str, Any] | None = None) -> None:
        (self.cfg / "settings.json").write_text(json.dumps(self.settings(peer, extra), indent=2))

    def start(self) -> None:
        env = {**os.environ, "HOME": str(self.home), "LLM_ORCH_CONFIG_DIR": str(self.cfg), "LLM_ORCH_DATA_DIR": str(self.data),
               "PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "FAKE_STATE": str(self.state),
               "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"), "PYTHONUNBUFFERED": "1"}
        (self.home / ".gitconfig").write_text("[user]\n\tname = E2E\n\temail = e2e@example.com\n[init]\n\tdefaultBranch = main\n")
        self.proc = subprocess.Popen([sys.executable, "-m", "server.app"], cwd=str(ROOT), env=env,
                                     stdout=self.log.open("ab"), stderr=subprocess.STDOUT, start_new_session=True)
        ok = wait_until(lambda: httpx.get(f"{self.url}/api/health", timeout=1).status_code == 200, timeout=30)
        if not ok:
            raise RuntimeError(f"{self.name} did not start; log:\n{self.log.read_text()[-3000:]}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None

    # ---- HTTP helpers (as a local client, like the Tauri app / browser on the machine) -------------
    def get(self, path: str, **kw) -> httpx.Response:
        return httpx.get(f"{self.url}{path}", timeout=kw.pop("timeout", 60), **kw)

    def post(self, path: str, json_body: Any = None, **kw) -> httpx.Response:
        headers = {"X-Orch-Client": "1", **(kw.pop("headers", None) or {})}
        return httpx.post(f"{self.url}{path}", json=json_body, headers=headers, timeout=kw.pop("timeout", 120), **kw)

    def machine_post(self, path: str, json_body: Any = None, **kw) -> httpx.Response:
        return httpx.post(f"{self.url}{path}", json=json_body, headers={"X-Node-Token": TOKEN}, timeout=kw.pop("timeout", 120), **kw)

    def wait_job(self, job_id: str, timeout: float = 60) -> dict[str, Any]:
        res = wait_until(lambda: (j := self.get(f"/api/jobs/{job_id}").json()) and j["status"] != "running" and j, timeout=timeout)
        return res or {"status": "timeout"}


class Cluster:
    def __init__(self, keep: bool = False):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-e2e-"))
        self.keep = keep
        self.llm = FakeLLM()
        ports: dict[str, int] = {}
        self.desktop = Node("desktop", "hub", self.tmp, self.llm, ports)
        self.laptop = Node("laptop", "worker", self.tmp, self.llm, ports)
        self.origin = self.tmp / "origin"

    def configure(self, desktop_extra: dict | None = None, laptop_extra: dict | None = None) -> None:
        self.desktop.write_settings(self.laptop, desktop_extra)
        self.laptop.write_settings(self.desktop, laptop_extra)

    def make_repo(self, name: str) -> tuple[Path, Path]:
        bare = self.origin / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
        seed = self.tmp / "seed" / name
        seed.mkdir(parents=True)
        git(seed, "init", "-b", "main")
        (seed / "app.py").write_text("print('v1')\n")
        (seed / ".gitignore").write_text("*.log\n")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "init")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-u", "origin", "main")
        clones = []
        for node in (self.desktop, self.laptop):
            dest = node.github / name
            subprocess.run(["git", "clone", "-q", str(bare), str(dest)], check=True, capture_output=True)
            clones.append(dest)
        return clones[0], clones[1]

    def start(self) -> None:
        self.desktop.start()
        self.laptop.start()

    def stop(self) -> None:
        for n in (self.desktop, self.laptop):
            n.stop()
        if not self.keep and not failures:
            shutil.rmtree(self.tmp, ignore_errors=True)
        elif failures:
            print(f"(kept {self.tmp} for inspection)")
