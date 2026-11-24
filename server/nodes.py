"""Generic node registry: peers with several candidate URLs (LAN, Tailscale), health, profiles.

Nothing here knows the words "laptop" or "desktop"; those are just peer ids from settings.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable

import httpx

HEALTH_TTL_SEC = 5.0


def peer_urls(peer: dict[str, Any]) -> list[str]:
    urls = list(peer.get("urls") or [])
    if peer.get("url"):
        urls.insert(0, peer["url"])
    return list(dict.fromkeys(str(u).rstrip("/") for u in urls if u))


class PeerOffline(RuntimeError):
    def __init__(self, peer_id: str):
        super().__init__(f"Node '{peer_id}' is offline or unreachable")
        self.peer_id = peer_id


class NodeRegistry:
    def __init__(self, get_settings: Callable[[], dict[str, Any]], token: Callable[[], str]):
        self.get_settings = get_settings
        self.token = token
        self._health: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}

    # ---- identity -----------------------------------------------------------------------------
    @property
    def local_id(self) -> str:
        return str(self.get_settings()["node"]["id"])

    @property
    def role(self) -> str:
        return str(self.get_settings()["node"].get("role") or "hub")

    def is_local(self, nid: str) -> bool:
        return nid in ("", "local", "auto", self.local_id)

    def peers(self) -> list[dict[str, Any]]:
        return [p for p in self.get_settings().get("peers") or [] if p.get("enabled", True) and p.get("id")]

    def peer(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.peers() if p["id"] == pid), None)

    def peer_ids(self) -> list[str]:
        return [p["id"] for p in self.peers()]

    def hub_id(self) -> str:
        if self.role == "hub":
            return self.local_id
        hub = next((p for p in self.peers() if p.get("role") == "hub"), None)
        return str(hub["id"]) if hub else self.local_id

    def headers(self) -> dict[str, str]:
        return {"X-Node-Token": self.token()}

    # ---- health ---------------------------------------------------------------------------------
    async def check(self, pid: str, force: bool = False) -> tuple[bool, str]:
        """Returns (online, base_url). Tries each candidate URL in order and remembers the winner."""
        peer = self.peer(pid)
        if not peer:
            return False, ""
        cached = self._health.get(pid)
        if cached and not force and time.time() - cached["checkedAt"] < HEALTH_TTL_SEC:
            return cached["online"], cached["url"]
        urls = peer_urls(peer)
        if cached and cached.get("url") in urls:
            urls.remove(cached["url"])
            urls.insert(0, cached["url"])
        online, chosen, info = False, (urls[0] if urls else ""), {}
        async with httpx.AsyncClient(timeout=1.5) as client:
            for u in urls:
                try:
                    r = await client.get(f"{u}/api/health", headers=self.headers())
                    if r.status_code == 200:
                        online, chosen, info = True, u, r.json()
                        break
                except Exception:
                    continue
        self._health[pid] = {"online": online, "url": chosen, "checkedAt": time.time(), "info": info}
        return online, chosen

    async def status_all(self) -> list[dict[str, Any]]:
        out = []
        results = await asyncio.gather(*(self.check(p["id"]) for p in self.peers()))
        for p, (ok, url) in zip(self.peers(), results):
            h = self._health.get(p["id"], {})
            out.append({**p, "online": ok, "activeUrl": url, "remoteInfo": h.get("info") or {}, "profile": self.profiles.get(p["id"])})
        return out

    def cached_online(self, pid: str) -> bool:
        return bool((self._health.get(pid) or {}).get("online"))

    # ---- requests ---------------------------------------------------------------------------------
    async def request(self, pid: str, method: str, path: str, *, timeout: float = 30.0, **kw: Any) -> httpx.Response:
        ok, url = await self.check(pid)
        if not ok:
            raise PeerOffline(pid)
        headers = {**self.headers(), **(kw.pop("headers", None) or {})}
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                return await client.request(method, f"{url}{path}", headers=headers, **kw)
            except httpx.TransportError:
                self._health.pop(pid, None)
                raise PeerOffline(pid)

    async def json(self, pid: str, method: str, path: str, **kw: Any) -> Any:
        r = await self.request(pid, method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{pid} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


# ---- Local capability profile ----------------------------------------------------------------------

def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def _gpus() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        gpus = []
        for line in p.stdout.splitlines():
            name, total, used = [x.strip() for x in line.split(",")]
            gpus.append({"name": name, "vramTotalMb": int(total), "vramUsedMb": int(used)})
        return gpus
    except Exception:
        return []


def _battery() -> dict[str, Any]:
    base = Path("/sys/class/power_supply")
    bats = sorted(base.glob("BAT*")) if base.is_dir() else []
    if not bats:
        return {"present": False, "onBattery": False}
    status = (bats[0] / "status").read_text().strip() if (bats[0] / "status").is_file() else ""
    cap = (bats[0] / "capacity").read_text().strip() if (bats[0] / "capacity").is_file() else ""
    return {"present": True, "onBattery": status.lower() == "discharging", "status": status, "capacityPct": int(cap) if cap.isdigit() else None}


def local_profile(settings: dict[str, Any]) -> dict[str, Any]:
    """Measured, not hard-coded: routing reads these numbers instead of node names."""
    mem = _meminfo()
    disks = {}
    for label, path in (("home", str(Path.home())), ("github", settings["paths"].get("githubDir") or "")):
        try:
            du = shutil.disk_usage(path)
            disks[label] = {"path": path, "totalGb": round(du.total / 1e9, 1), "freeGb": round(du.free / 1e9, 1)}
        except OSError:
            continue
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "nodeId": settings["node"]["id"],
        "role": settings["node"].get("role"),
        "alwaysOn": bool(settings["node"].get("alwaysOn", True)),
        "ramTotalGb": round(mem.get("MemTotal", 0) / 1e9, 1),
        "ramAvailableGb": round(mem.get("MemAvailable", 0) / 1e9, 1),
        "gpus": _gpus(),
        "disks": disks,
        "battery": _battery(),
        "cpuCount": os.cpu_count(),
        "load1": round(load[0], 2),
        "measuredAt": time.time(),
    }
