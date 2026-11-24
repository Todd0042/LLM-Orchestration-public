"""Service health watchdog, phone alerts, SQLite backups to the other node, and site failover.

Site watchdog. What it guarantees:
  * restarts only after `failThreshold` failed checks in a row, with exponential backoff, and
    gives up (and tells you) after `maxRestartsPerHour` instead of looping forever
  * blue/green unit groups restart one at a time, waiting for health in between
  * alerts go to your phone (ntfy) and the dashboard, rate-limited per subject
  * one list (settings.services) drives health checks, backups and failover
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any

import httpx

DEFAULTS = {"intervalSec": 30, "failThreshold": 3, "maxRestartsPerHour": 4, "backoffBaseSec": 60, "backoffMaxSec": 1800}


class Alerts:
    def __init__(self, ctx):
        self.ctx = ctx
        self._last: dict[str, float] = {}

    async def send(self, title: str, body: str, *, key: str = "", priority: str = "default") -> bool:
        cfg = self.ctx.settings().get("alerts") or {}
        k = key or title
        now = time.time()
        if now - self._last.get(k, 0) < float(cfg.get("minIntervalSec", 300)):
            return False
        self._last[k] = now
        await self.ctx.emit({"type": "alert", "title": title, "body": body, "priority": priority, "nodeId": self.ctx.nodes.local_id})
        self.ctx.audit.record("alert", title, body=body[:500])
        url = str(cfg.get("ntfyUrl") or "").strip()
        if not url.startswith("https://"):
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(url, content=body.encode()[:4000], headers={
                    "Title": f"[{self.ctx.nodes.local_id}] {title}"[:200].encode("ascii", "replace").decode(),
                    "Priority": priority, "Tags": "rotating_light" if priority in ("high", "urgent") else "information_source"})
            return True
        except Exception:
            return False


async def _run(*argv: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=16 * 1024 * 1024)
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode or 0, out.decode(errors="replace").strip()


def resolve_container(svc: dict[str, Any]) -> str:
    spec = svc.get("containerFrom") or {}
    if spec.get("file"):
        try:
            m = re.search(str(spec.get("regex")), Path(spec["file"]).expanduser().read_text())
            if m:
                return str((spec.get("map") or {}).get(m.group(1)) or svc.get("container") or "")
        except (OSError, re.error):
            pass
    return str(svc.get("container") or "")


class ServiceMonitor:
    def __init__(self, ctx):
        self.ctx = ctx

    def services(self) -> list[dict[str, Any]]:
        me = self.ctx.nodes.local_id
        return [s for s in self.ctx.settings().get("services") or [] if s.get("id") and s.get("node", me) == me and s.get("enabled", True)]

    def cfg(self) -> dict[str, Any]:
        return {**DEFAULTS, **(self.ctx.settings().get("monitor") or {})}

    # ---- checks ---------------------------------------------------------------------------------
    async def check(self, svc: dict[str, Any]) -> tuple[bool, str]:
        problems: list[str] = []
        for unit in svc.get("units") or []:
            rc, out = await _run("systemctl", "--user", "is-active", unit, timeout=10)
            if out.strip() != "active":
                problems.append(f"{unit} is {out.strip() or rc}")
        container = resolve_container(svc)
        if container:
            rc, out = await _run("docker", "inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", container, timeout=15)
            if rc != 0:
                problems.append(f"container {container} missing")
            elif not out.startswith("running") or "unhealthy" in out:
                problems.append(f"container {container}: {out}")
        h = svc.get("health") or {}
        if h.get("url"):
            ok_codes = set(h.get("okCodes") or [])
            try:
                async with httpx.AsyncClient(timeout=float(h.get("timeoutSec", 8)), follow_redirects=False) as c:
                    r = await c.get(h["url"], headers={"User-Agent": "llm-orchestration-monitor"})
                if not (200 <= r.status_code < 400 or r.status_code in ok_codes):
                    problems.append(f"{h['url']} -> HTTP {r.status_code}")
            except Exception as exc:
                problems.append(f"{h['url']} -> {type(exc).__name__}")
        return (not problems), "; ".join(problems) or "healthy"

    # ---- state --------------------------------------------------------------------------------
    def _state(self, sid: str) -> dict[str, Any]:
        r = self.ctx.conn.execute("SELECT * FROM service_state WHERE service_id=? AND node_id=?", (sid, self.ctx.nodes.local_id)).fetchone()
        if not r:
            return {"status": "unknown", "consecutive_failures": 0, "restarts": [], "next_action_at": 0.0, "last_detail": ""}
        return {"status": r["status"], "consecutive_failures": r["consecutive_failures"], "restarts": json.loads(r["restarts_json"]),
                "next_action_at": r["next_action_at"], "last_detail": r["last_detail"]}

    def _save(self, sid: str, st: dict[str, Any]) -> None:
        self.ctx.conn.execute(
            "INSERT OR REPLACE INTO service_state (service_id, node_id, status, consecutive_failures, restarts_json, next_action_at, last_detail, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, self.ctx.nodes.local_id, st["status"], st["consecutive_failures"], json.dumps(st["restarts"]), st["next_action_at"], st["last_detail"][:500], time.time()))
        self.ctx.conn.commit()

    def _event(self, sid: str, kind: str, detail: str) -> None:
        self.ctx.conn.execute("INSERT INTO service_events (ts, node_id, service_id, kind, detail) VALUES (?,?,?,?,?)",
                              (time.time(), self.ctx.nodes.local_id, sid, kind, detail[:1000]))
        self.ctx.conn.commit()

    # ---- repair -------------------------------------------------------------------------------
    async def restart(self, svc: dict[str, Any]) -> str:
        notes = []
        for unit in svc.get("restartOrder") or svc.get("units") or []:
            rc, out = await _run("systemctl", "--user", "restart", unit, timeout=60)
            notes.append(f"restart {unit}: rc={rc}{(' ' + out[:120]) if rc else ''}")
            if len(svc.get("restartOrder") or []) > 1:
                # blue/green: wait (bounded) for this slot before touching the next one
                for _ in range(max(1, int(float(svc.get("slotWaitSec", 20)) / 0.5))):
                    await asyncio.sleep(0.5)
                    rc2, out2 = await _run("systemctl", "--user", "is-active", unit, timeout=10)
                    if out2.strip() == "active":
                        break
        container = resolve_container(svc)
        if container:
            rc, out = await _run("docker", "restart", container, timeout=90)
            if rc != 0 and (svc.get("compose") or {}).get("dir"):
                comp = svc["compose"]
                rc, out = await _run("docker", "compose", "--project-directory", str(Path(comp["dir"]).expanduser()), "up", "-d", str(comp.get("service") or ""), timeout=180)
                notes.append(f"compose up {comp.get('service')}: rc={rc}")
            else:
                notes.append(f"docker restart {container}: rc={rc}")
        return "; ".join(notes) or "nothing to restart (health-only service)"

    async def tick(self) -> list[dict[str, Any]]:
        cfg = self.cfg()
        now = time.time()
        report = []
        for svc in self.services():
            sid = svc["id"]
            ok, detail = await self.check(svc)
            st = self._state(sid)
            st["restarts"] = [t for t in st["restarts"] if now - t < 3600]
            prev = st["status"]
            if ok:
                if prev in ("failing", "gave_up", "restarting"):
                    self._event(sid, "recovered", detail)
                    await self.ctx.alerts.send(f"{svc.get('name', sid)} recovered", detail, key=f"svc:{sid}:up")
                st.update(status="healthy", consecutive_failures=0, last_detail=detail, next_action_at=0.0)
            else:
                st["consecutive_failures"] += 1
                st["last_detail"] = detail
                st["status"] = "failing" if prev != "gave_up" else "gave_up"
                auto = svc.get("autoRestart", True)
                if auto and st["consecutive_failures"] >= int(cfg["failThreshold"]) and now >= st["next_action_at"] and st["status"] != "gave_up":
                    if len(st["restarts"]) >= int(cfg["maxRestartsPerHour"]):
                        st["status"] = "gave_up"
                        self._event(sid, "gave_up", detail)
                        await self.ctx.alerts.send(f"{svc.get('name', sid)} is down; stopped retrying",
                                                   f"{len(st['restarts'])} restarts in the last hour didn't fix it: {detail}", key=f"svc:{sid}:gave_up", priority="high")
                    else:
                        note = await self.restart(svc)
                        st["restarts"].append(now)
                        st["status"] = "restarting"
                        backoff = min(float(cfg["backoffMaxSec"]), float(cfg["backoffBaseSec"]) * (2 ** (len(st["restarts"]) - 1)))
                        st["next_action_at"] = now + backoff
                        self._event(sid, "restarted", f"{detail} -> {note}")
                        self.ctx.audit.record("service_restart", sid, detail=detail, note=note)
                        await self.ctx.alerts.send(f"{svc.get('name', sid)} restarted", f"{detail}\n{note}", key=f"svc:{sid}:restart")
                elif st["consecutive_failures"] == int(cfg["failThreshold"]) and not auto:
                    await self.ctx.alerts.send(f"{svc.get('name', sid)} is failing", detail, key=f"svc:{sid}:fail", priority="high")
            self._save(sid, st)
            report.append({"id": sid, "name": svc.get("name", sid), "ok": ok, **{k: st[k] for k in ("status", "consecutive_failures", "last_detail")},
                           "restartsLastHour": len(st["restarts"]), "publicHost": svc.get("publicHost", "")})
        return report

    def status(self) -> list[dict[str, Any]]:
        rows = self.ctx.conn.execute("SELECT * FROM service_state WHERE node_id=?", (self.ctx.nodes.local_id,)).fetchall()
        by_id = {r["service_id"]: r for r in rows}
        out = []
        for svc in self.services():
            r = by_id.get(svc["id"])
            out.append({"id": svc["id"], "name": svc.get("name", svc["id"]), "publicHost": svc.get("publicHost", ""),
                        "status": r["status"] if r else "unknown", "detail": r["last_detail"] if r else "",
                        "consecutiveFailures": r["consecutive_failures"] if r else 0,
                        "restartsLastHour": len([t for t in json.loads(r["restarts_json"]) if time.time() - t < 3600]) if r else 0,
                        "failover": (svc.get("failover") or {}).get("mode", "maintenance"), "backups": len((svc.get("backup") or {}).get("sqlite") or [])})
        return out

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.ctx.conn.execute("SELECT * FROM service_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---- Backups: consistent SQLite snapshots shipped to the other node -------------------------------

def sqlite_snapshot_gz(src: Path) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "snap.db"
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        return gzip.compress(tmp.read_bytes(), compresslevel=6)


def store_backup(root: Path, node: str, service: str, name: str, data: bytes, keep: int = 14) -> Path:
    safe = lambda x: re.sub(r"[^A-Za-z0-9._-]", "_", x)[:80] or "x"  # noqa: E731
    d = root / safe(node) / safe(service)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    dest = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe(name)}.gz"
    dest.write_bytes(data)
    os.chmod(dest, 0o600)
    olds = sorted(d.glob(f"*-{safe(name)}.gz"))
    for old in olds[:-keep]:
        old.unlink(missing_ok=True)
    return dest


class BackupManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self._last_run = 0.0
        self.last: dict[str, Any] = {}

    def target(self) -> str:
        return str((self.ctx.settings().get("backups") or {}).get("targetNode") or "")

    async def run(self, force: bool = False) -> dict[str, Any]:
        cfg = self.ctx.settings().get("backups") or {}
        interval = float(cfg.get("intervalSec", 6 * 3600))
        if not force and time.time() - self._last_run < interval:
            return {"skipped": "not due"}
        self._last_run = time.time()
        target = self.target()
        results: dict[str, Any] = {}
        for svc in ServiceMonitor(self.ctx).services():
            for path in (svc.get("backup") or {}).get("sqlite") or []:
                p = Path(path).expanduser()
                key = f"{svc['id']}:{p.name}"
                if not p.is_file():
                    results[key] = "missing"
                    continue
                try:
                    data = await asyncio.to_thread(sqlite_snapshot_gz, p)
                    if target and not self.ctx.nodes.is_local(target):
                        r = await self.ctx.nodes.request(target, "POST", "/api/internal/backups/upload", timeout=300.0, content=data,
                                                         params={"node": self.ctx.nodes.local_id, "service": svc["id"], "name": p.name},
                                                         headers={"Content-Type": "application/gzip"})
                        results[key] = "sent" if r.status_code == 200 else f"HTTP {r.status_code}"
                    else:
                        store_backup(self.ctx.backups_dir(), self.ctx.nodes.local_id, svc["id"], p.name, data, int(cfg.get("keep", 14)))
                        results[key] = "stored locally"
                except Exception as exc:
                    results[key] = f"error: {exc}"[:200]
        failed = {k: v for k, v in results.items() if v not in ("sent", "stored locally")}
        if failed:
            await self.ctx.alerts.send("Backups had problems", json.dumps(failed)[:1500], key="backups")
        self.last = {"at": time.time(), "target": target, "results": results}
        self.ctx.audit.record("backups", target, results=results)
        return self.last


# ---- Failover: the standby node takes over public sites while the primary is down ------------------

class FailoverController:
    def __init__(self, ctx):
        self.ctx = ctx
        self.state = "standby"
        self._down_since: float | None = None
        self._up_since: float | None = None

    def cfg(self) -> dict[str, Any]:
        return self.ctx.settings().get("failover") or {}

    async def _primary_alive(self) -> bool:
        cfg = self.cfg()
        primary = str(cfg.get("primaryNode") or "")
        ok, _ = await self.ctx.nodes.check(primary, force=True)
        if ok:
            return True
        probe = str(cfg.get("publicProbeUrl") or "")
        if probe:  # LAN/Tailscale can fail while the primary is fine (e.g. you're travelling)
            try:
                async with httpx.AsyncClient(timeout=8) as c:
                    r = await c.get(probe)
                    if r.status_code == 200 and str(r.json().get("nodeId")) == primary:
                        return True
            except Exception:
                pass
        return False

    async def _internet_ok(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                url = str(self.cfg().get("internetProbeUrl") or "https://www.cloudflare.com/cdn-cgi/trace")
                return (await c.get(url)).status_code == 200
        except Exception:
            return False

    async def tick(self) -> dict[str, Any]:
        cfg = self.cfg()
        if not cfg.get("enabled") or cfg.get("role") != "standby":
            return {"state": "disabled"}
        alive = await self._primary_alive()
        now = time.time()
        if alive:
            self._down_since = None
            self._up_since = self._up_since or now
            if self.state == "active" and now - self._up_since >= float(cfg.get("upAfterSec", 180)):
                await self._deactivate()
        else:
            self._up_since = None
            self._down_since = self._down_since or now
            if self.state == "standby" and now - self._down_since >= float(cfg.get("downAfterSec", 120)) and await self._internet_ok():
                await self._activate()
        return {"state": self.state, "primaryAlive": alive, "downSince": self._down_since, "upSince": self._up_since}

    async def _units(self) -> list[str]:
        units = [str(self.cfg().get("connectorUnit") or "")]
        for svc in self.ctx.settings().get("services") or []:
            fo = svc.get("failover") or {}
            if fo.get("mode") == "full" and fo.get("standbyUnit"):
                units.append(str(fo["standbyUnit"]))
        return [u for u in units if u]

    async def _activate(self) -> None:
        notes = [f"start {u}: rc={(await _run('systemctl', '--user', 'start', u))[0]}" for u in await self._units()]
        self.state = "active"
        self.ctx.audit.record("failover_activate", "", notes=notes)
        await self.ctx.alerts.send("Failover ACTIVE: this machine is serving your sites",
                                   f"{self.cfg().get('primaryNode')} has been unreachable. " + "; ".join(notes), key="failover", priority="urgent")

    async def _deactivate(self) -> None:
        notes = [f"stop {u}: rc={(await _run('systemctl', '--user', 'stop', u))[0]}" for u in reversed(await self._units())]
        self.state = "standby"
        self.ctx.audit.record("failover_deactivate", "", notes=notes)
        await self.ctx.alerts.send("Failover ended", f"{self.cfg().get('primaryNode')} is back; standby stopped. " + "; ".join(notes), key="failover", priority="high")


MAINTENANCE_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Back soon</title><style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;height:100vh;margin:0}
main{max-width:28rem;text-align:center;padding:2rem}h1{font-size:1.6rem}p{color:#9aa3b2}</style></head>
<body><main><h1>Back soon</h1><p>This site's main server is offline for a moment. A backup machine is answering in the meantime; please try again shortly.</p></main></body></html>"""


def which(name: str) -> bool:
    return shutil.which(name) is not None
