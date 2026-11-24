"""AppContext: the one object that owns every service. Routes and services receive it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sqlite3
import stat
import os
from typing import Any

from .agent_chain import AgentAvailability
from .agents import AgentDispatcher
from .auth import Guard, TotpVerifier
from .identity import DeviceSessions, PairingLinks, Passkeys
from .config import DATA_DIR, ensure_node_token, init_db, load_settings, merge_settings_update, save_settings
from .core import ApprovalBroker, AuditLog, Broadcaster, JobRunner
from .gpu_and_media import GpuBroker
from .inventory_sync import enqueue_intermission_event, scan_local_repos
from .llm import GpuTurns, LlmRouter
from .monitor import Alerts, BackupManager, FailoverController, ServiceMonitor
from .nodes import NodeRegistry, PeerOffline, local_profile
from .orchestrator import Orchestrator
from .pty_runner import TerminalManager
from .replication import Replicator
from .repo_sync import RepoSync
from .repo_tracker import hold_counts, ingest_peer_tracker_payload, init_tracker_db, repo_status_db_path, scan_all_repos_tracker
from .security import MediaSigner, SecurityPolicy, TicketStore


class AppContext:
    def __init__(self, *, cfg_dir: Path, data_dir: Path, db_path: Path, dist_dir: Path):
        self.cfg_dir = cfg_dir
        self.data_dir = data_dir
        self.dist_dir = dist_dir
        self.settings_path = cfg_dir / "settings.json"
        self._token = ensure_node_token(cfg_dir)
        self._settings = load_settings(self.settings_path)
        self.conn: sqlite3.Connection = init_db(db_path)
        self.tracker_conn: sqlite3.Connection = init_tracker_db(repo_status_db_path(data_dir))

        self.emit = Broadcaster()
        self.nodes = NodeRegistry(self.settings, lambda: self._token)
        self.audit = AuditLog(self.conn, lambda: self.nodes.local_id)
        self.jobs = JobRunner(self.conn, self.emit, lambda: self.nodes.local_id)
        self.approvals = ApprovalBroker(self.emit)
        self.alerts = Alerts(self)
        self.availability = AgentAvailability()

        auth = self._settings.get("auth") or {}
        totp_dir = Path(str(auth.get("totpStateDir") or DATA_DIR)).expanduser()
        self.totp = TotpVerifier(totp_dir / "totp_secret", data_dir / "last_totp_step")
        self.guard = Guard(max_per_ip=5, lockout_sec=900, max_global=30)
        self.sessions = DeviceSessions(self.conn)
        self.passkeys = Passkeys(self.conn)
        self.pairing = PairingLinks()
        self.policy = SecurityPolicy(self.settings, tunnel_port=int(self._settings["node"].get("tunnelPort", 8791)))
        self.signer = MediaSigner(self._token)
        self.tickets = TicketStore()

        self.term = TerminalManager(node_id=self.nodes.local_id, emit_event=self.emit,
                                    timeout_sec=int(self._settings["terminal"].get("sudoTimeoutSec", 60)),
                                    runtime_dir=data_dir / "runtime")
        self.gpu = GpuTurns(self.settings)
        self.broker = GpuBroker(self.gpu)
        self.llm = LlmRouter(nodes=self.nodes, get_settings=self.settings, conn=self.conn, gpu=self.gpu)
        self.agents = AgentDispatcher(nodes=self.nodes, get_settings=self.settings, emit=self.emit, askpass_env=self.askpass_env,
                                      config_dir=cfg_dir, on_local_turn=self._on_local_turn, audit=self.audit)
        self.repo_sync = RepoSync(self)
        self.orchestrator = Orchestrator(self)
        self.replicator = Replicator(self)
        self.monitor = ServiceMonitor(self)
        self.backups = BackupManager(self)
        self.failover = FailoverController(self)
        self._loops: list[asyncio.Task] = []

    # ---- settings & paths ------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self._settings

    def token(self) -> str:
        return self._token

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        new = merge_settings_update(self._settings, body)
        save_settings(new, self.settings_path)
        self._settings = new
        self.term.node_id = self.nodes.local_id
        self.term.bridge.timeout_sec = int(new["terminal"].get("sudoTimeoutSec", 60))
        return new

    def rotate_token(self, new_token: str | None = None) -> str:
        tok = new_token or secrets.token_urlsafe(32)
        path = self.cfg_dir / "node_token"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(tok + "\n", encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)
        self._token = tok
        self.signer = MediaSigner(tok)
        return tok

    def github_dir(self) -> Path:
        return Path(str(self._settings["paths"].get("githubDir") or (Path.home() / "GitHub"))).expanduser()

    def vault_dir(self) -> Path:
        return Path(str(self._settings["paths"].get("vaultDir") or (Path.home() / "LLM-Vault"))).expanduser()

    def backups_dir(self) -> Path:
        return Path(str(self._settings["paths"].get("backupsDir") or (Path.home() / "Backups" / "llm-orchestration"))).expanduser()

    def askpass_env(self) -> dict[str, str]:
        return {"SUDO_ASKPASS": str(self.term.askpass_path), "LLM_ORCH_ASKPASS_SOCK": str(self.term.sock_path), "PATH": str(self.term.shim_bin)}

    # ---- hooks ---------------------------------------------------------------------------------------
    async def rescan_repos(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(scan_local_repos, self.conn, self.github_dir(), self.nodes.local_id)

    async def rescan_tracker(self, check_remote: bool = True) -> list[dict[str, Any]]:
        items = await asyncio.to_thread(
            scan_all_repos_tracker,
            self.tracker_conn,
            self.github_dir(),
            self.nodes.local_id,
            data_dir=self.data_dir,
            vault_dir=self.vault_dir(),
            check_remote=check_remote,
        )
        for pid in self.nodes.peer_ids():
            try:
                peer_data = await self.nodes.json(pid, "GET", "/api/internal/repo-tracker", timeout=25.0)
                ingest_peer_tracker_payload(self.tracker_conn, pid, peer_data, data_dir=self.data_dir, vault_dir=self.vault_dir())
            except Exception:
                continue
        return items

    async def _on_local_turn(self, workdir: str, agent: str, source: str) -> None:
        try:
            res = await self.repo_sync.preflight(cwd=workdir, source=f"{agent}:{source}")
            if res.get("message"):
                await self.emit({"type": "repo_preflight", "cwd": workdir, **{k: v for k, v in res.items() if k in ("status", "message")}})
        except Exception:
            pass

    async def catchup(self, peer: str) -> dict[str, int]:
        """Replay actions queued for `peer` while it was offline (git pulls etc.)."""
        rows = self.conn.execute("SELECT * FROM desktop_intermission_queue WHERE status='queued' AND target_node=? ORDER BY id", (peer,)).fetchall()
        done = conflicts = 0
        for r in rows:
            if r["category"] in ("repo_sync", "repo_commit"):
                try:
                    res = await self.nodes.json(peer, "POST", "/api/sync/git-pull", json={"repoName": r["target"]}, timeout=120.0)
                    status = "synced" if res.get("ok") else "conflict_needs_agent"
                    note = str(res.get("message") or "")
                except PeerOffline:
                    break
                except Exception as exc:
                    status, note = "conflict_needs_agent", str(exc)[:200]
            else:
                status, note = "synced", "covered by replication"
            conflicts += status != "synced"
            done += 1
            self.conn.execute("UPDATE desktop_intermission_queue SET status=?, result_note=?, synced_at=strftime('%s','now') WHERE id=?", (status, note, r["id"]))
            self.conn.commit()
        return {"processed": done, "conflicts": conflicts}

    def enqueue_for(self, peer: str, category: str, target: str, payload: dict[str, Any] | None = None) -> int:
        eid = enqueue_intermission_event(self.conn, category, target, payload)
        self.conn.execute("UPDATE desktop_intermission_queue SET target_node=? WHERE id=?", (peer, eid))
        self.conn.commit()
        return eid

    # ---- background loops ------------------------------------------------------------------------------
    def _loop(self, name: str, interval: float, fn) -> None:
        async def runner() -> None:
            await asyncio.sleep(2.0)
            while True:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a broken loop must not take the server down
                    self.audit.record("loop_error", name, error=str(exc)[:500])
                await asyncio.sleep(interval)
        self._loops.append(asyncio.create_task(runner(), name=name))

    async def _refresh_profiles(self) -> None:
        self.nodes.profiles[self.nodes.local_id] = {**await asyncio.to_thread(local_profile, self._settings), "githubDir": str(self.github_dir())}
        for pid in self.nodes.peer_ids():
            try:
                self.nodes.profiles[pid] = await self.nodes.json(pid, "GET", "/api/node/profile", timeout=10.0)
            except Exception:
                pass

    async def start(self, background: bool = True) -> None:
        await self.term.start()
        self.jobs.mark_orphans()
        self.vault_dir().mkdir(parents=True, exist_ok=True)
        await self.rescan_repos()
        if background:
            self._loop("profiles", 60.0, self._refresh_profiles)
            self._loop("replication", float((self._settings.get("replication") or {}).get("intervalSec", 20)), self.replicator.run_once)
            self._loop("monitor", float((self._settings.get("monitor") or {}).get("intervalSec", 30)), self.monitor.tick)
            self._loop("backups", 300.0, self.backups.run)
            self._loop("failover", 20.0, self.failover.tick)
            self._loop("repo_scan", 120.0, self.rescan_repos)
            self._loop("repo_tracker", 1500.0, self.rescan_tracker)

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await self.jobs.shutdown()
        await self.term.stop()
        self.tracker_conn.close()
        self.conn.close()

    def describe(self) -> dict[str, Any]:
        return json.loads(json.dumps({"node": self._settings["node"], "peers": self.nodes.peer_ids()}))
