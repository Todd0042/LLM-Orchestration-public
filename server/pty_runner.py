"""Standalone askpass helper + PTY script runner with pop-up sudo password bridge."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import stat
import threading
from typing import Awaitable, Callable
import uuid

from .config import DATA_DIR



def ensure_askpass_wrappers(runtime_dir: Path) -> tuple[Path, Path]:
    """Create executable SUDO_ASKPASS script + PATH sudo shim forwarding to our IPC socket."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    askpass = runtime_dir / "llm-orch-askpass"
    askpass.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, socket, sys\n"
        "sock_path = os.environ['LLM_ORCH_ASKPASS_SOCK']\n"
        "prompt = sys.argv[1] if len(sys.argv) > 1 else '[sudo] password:'\n"
        "with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\n"
        "    s.connect(sock_path)\n"
        "    s.sendall((json.dumps({'prompt': prompt, 'runId': os.environ.get('LLM_ORCH_RUN_ID', ''), 'cmd': os.environ.get('LLM_ORCH_CMD', '')}) + '\\n').encode())\n"
        "    data = b''\n"
        "    while True:\n"
        "        chunk = s.recv(4096)\n"
        "        if not chunk:\n"
        "            break\n"
        "        data += chunk\n"
        "resp = json.loads(data.decode() or '{}')\n"
        "if not resp.get('ok'):\n"
        "    sys.exit(1)\n"
        "sys.stdout.write(resp.get('password', '') + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    os.chmod(askpass, 0o700)

    shim_bin = runtime_dir / "bin"
    shim_bin.mkdir(parents=True, exist_ok=True)
    sudo_shim = shim_bin / "sudo"
    sudo_shim.write_text(
        "#!/usr/bin/env bash\n"
        "for a in \"$@\"; do\n"
        "  if [[ \"$a\" == \"-n\" || \"$a\" == \"-A\" ]]; then exec /usr/bin/sudo \"$@\"; fi\n"
        "done\n"
        "exec /usr/bin/sudo -A \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(sudo_shim, 0o700)
    return askpass, shim_bin


class PasswordBridge:
    """In-memory-only coordinator for interactive sudo/PTY password requests."""

    def __init__(self, timeout_sec: int = 60):
        self.timeout_sec = timeout_sec
        self._pending: dict[str, tuple[asyncio.Future[str | None], asyncio.AbstractEventLoop]] = {}
        self._lock = threading.Lock()

    async def request_password(
        self,
        emit_event: Callable[[dict], Awaitable[None]],
        node_id: str,
        command: str,
        prompt_text: str,
        run_id: str,
    ) -> str | None:
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        with self._lock:
            self._pending[req_id] = (fut, loop)

        await emit_event(
            {
                "type": "password_prompt",
                "requestId": req_id,
                "runId": run_id,
                "nodeId": node_id,
                "command": command,
                "prompt": prompt_text,
                "timeoutSec": self.timeout_sec,
            }
        )
        try:
            return await asyncio.wait_for(fut, timeout=float(self.timeout_sec))
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def resolve(self, req_id: str, password: str | None) -> bool:
        with self._lock:
            entry = self._pending.pop(req_id, None)
        if not entry:
            return False
        fut, loop = entry
        loop.call_soon_threadsafe(lambda: fut.set_result(password) if not fut.done() else None)
        return True


class TerminalManager:
    """Runs terminal commands inside a real PTY and connects askpass IPC -> UI pop-up."""

    def __init__(
        self,
        node_id: str,
        emit_event: Callable[[dict], Awaitable[None]],
        timeout_sec: int = 60,
        runtime_dir: Path | None = None,
    ):
        self.node_id = node_id
        self.emit_event = emit_event
        self.runtime_dir = runtime_dir or (DATA_DIR / "runtime")
        self.askpass_path, self.shim_bin = ensure_askpass_wrappers(self.runtime_dir)
        self.sock_path = self.runtime_dir / f"askpass_{os.getpid()}.sock"
        self.bridge = PasswordBridge(timeout_sec=timeout_sec)
        self._ipc_server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.sock_path.unlink(missing_ok=True)
        self._ipc_server = await asyncio.start_unix_server(self._handle_askpass_conn, path=str(self.sock_path))
        os.chmod(self.sock_path, stat.S_IRUSR | stat.S_IWUSR)

    async def stop(self) -> None:
        if self._ipc_server:
            self._ipc_server.close()
            await self._ipc_server.wait_closed()
        self.sock_path.unlink(missing_ok=True)

    async def _handle_askpass_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            info = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            pw = await self.bridge.request_password(
                self.emit_event,
                node_id=self.node_id,
                command=str(info.get("cmd") or "sudo command"),
                prompt_text=str(info.get("prompt") or "[sudo] password:"),
                run_id=str(info.get("runId") or ""),
            )
            payload = {"ok": pw is not None, "password": pw or ""}
            writer.write(json.dumps(payload).encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def run_command(self, command: str, cwd: str = "", shell: str = "/usr/bin/bash", chat_id: str = "") -> dict:
        run_id = uuid.uuid4().hex[:10]
        workdir = cwd.strip() or str(Path.home())
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env["SUDO_ASKPASS"] = str(self.askpass_path)
        env["LLM_ORCH_ASKPASS_SOCK"] = str(self.sock_path)
        env["LLM_ORCH_RUN_ID"] = run_id
        env["LLM_ORCH_CMD"] = command
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env["SYSTEMD_PAGER"] = ""
        env["PATH"] = f"{self.shim_bin}:{env.get('PATH', '')}"

        proc = await asyncio.create_subprocess_exec(
            shell,
            "-lc",
            command,
            cwd=workdir,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            limit=16 * 1024 * 1024,
            env=env,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        output_chunks: list[str] = []
        loop = asyncio.get_running_loop()
        done_ev = asyncio.Event()

        def on_pty_read() -> None:
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(master_fd)
                done_ev.set()
                return
            text = data.decode("utf-8", errors="replace")
            output_chunks.append(text)
            asyncio.create_task(
                self.emit_event({"type": "terminal_output", "runId": run_id, "chatId": chat_id, "nodeId": self.node_id, "chunk": text})
            )

        loop.add_reader(master_fd, on_pty_read)
        rc = await proc.wait()
        try:
            await asyncio.wait_for(done_ev.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            loop.remove_reader(master_fd)
        os.close(master_fd)

        full_out = "".join(output_chunks)
        await self.emit_event(
            {"type": "terminal_done", "runId": run_id, "chatId": chat_id, "nodeId": self.node_id, "exitCode": rc, "output": full_out}
        )
        return {"runId": run_id, "exitCode": rc, "output": full_out}
