#!/usr/bin/env python3
"""End-to-end scenarios for LLM-Orchestration across two real nodes.

    python3 e2e/test_two_nodes.py            # all scenarios
    python3 e2e/test_two_nodes.py security   # just one (security|orchestrator|handoff|replication|agents|watchdog|failover)
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, TOKEN, TOTP_SECRET, Cluster, StatusServer, check, failures, git, wait_until  # noqa: E402

sys.path.insert(0, str(ROOT))
from server.auth import _hotp  # noqa: E402


def totp_now(offset_steps: int = 0) -> str:
    return _hotp(TOTP_SECRET, int(time.time() // 30) + offset_steps)


# ---------------------------------------------------------------------------------------------------
def scenario_security(c: Cluster) -> None:
    print("\n== Security: origins, hosts, tunnel login, tokens, file roots, media, tickets ==")
    d = c.desktop
    r = httpx.get(f"{d.url}/api/settings", headers={"Origin": "https://evil.example"})
    check(r.status_code == 403, "a website on another origin can't read the API from a local browser")
    r = httpx.post(f"{d.url}/api/terminal/run", json={"command": "touch /tmp/pwned"}, headers={"Origin": "https://evil.example", "X-Orch-Client": "1"})
    check(r.status_code == 403 and not Path("/tmp/pwned").exists(), "cross-origin command execution is refused")
    r = httpx.options(f"{d.url}/api/terminal/run", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    check(r.status_code == 403 and "access-control-allow-origin" not in r.headers, "CORS preflight from a foreign origin is refused")
    r = httpx.post(f"{d.url}/api/chats/native", json={}, headers={"Content-Type": "text/plain"})
    check(r.status_code == 403, "state-changing request without the client header is refused (CSRF)")
    r = httpx.get(f"{d.url}/api/settings", headers={"Host": "attacker-rebind.example:8790"})
    check(r.status_code == 421, "DNS-rebinding Host header is refused")
    r = httpx.get(f"{d.url}/api/settings", headers={"Origin": "tauri://localhost"})
    check(r.status_code == 200 and r.headers.get("access-control-allow-origin") == "tauri://localhost", "the Tauri app's origin is allowed")
    check("nodeToken" not in r.text and TOKEN not in r.text, "settings never include the node token")

    # Tunnel listener: only a 6-digit-code session works
    t = d.tunnel
    check(httpx.get(f"{t}/api/settings").status_code == 401, "tunnel port: no session -> 401")
    check(httpx.get(f"{t}/api/settings", headers={"X-Node-Token": TOKEN}).status_code == 401, "tunnel port: the node token is NOT accepted")
    st = httpx.get(f"{t}/api/auth/status").json()
    check(st["authRequired"] and not st["authenticated"] and st["zone"] == "tunnel", "tunnel status asks for the code")
    with httpx.Client(base_url=t, headers={"X-Orch-Client": "1"}) as web:
        bad = web.post("/api/auth/login", json={"code": "000000"})
        check(bad.status_code == 401, "wrong code refused")
        code = totp_now()
        ok = web.post("/api/auth/login", json={"code": code})
        set_cookie = ok.headers.get("set-cookie", "")
        check(ok.status_code == 200 and "orch_session=" in set_cookie, "correct code logs in and sets the session cookie")
        check("HttpOnly" in set_cookie and "Secure" in set_cookie and "SameSite=strict" in set_cookie, "tunnel session cookie is HttpOnly + Secure + SameSite=strict")
        # the real tunnel is HTTPS; this test talks plain HTTP, so send the Secure cookie by hand
        web.headers["Cookie"] = set_cookie.split(";", 1)[0]
        replay = httpx.post(f"{t}/api/auth/login", json={"code": code}, headers={"X-Orch-Client": "1"})
        check(replay.status_code == 401, "the same code can't be replayed")
        check(web.get("/api/settings").status_code == 200, "session works on the tunnel")
        r = web.put("/api/settings", json={"auth": {"requireAuthEverywhere": False, "sessionIdleSec": 99999}})
        check(r.status_code == 403, "auth settings can't be changed over the tunnel")
        r = web.put("/api/settings", json={"peers": [{"id": "laptop", "url": "https://attacker.example"}]})
        check(r.status_code == 403, "peer URLs (where the token is sent) can't be changed over the tunnel")
        r = web.put("/api/settings", json={"taskDefaults": {"story": {"temperature": 0.9}}})
        check(r.status_code == 200 and r.json()["settings"]["taskDefaults"]["story"]["temperature"] == 0.9, "ordinary settings can be changed over the tunnel")
        r = web.post("/api/internal/agent_turn", json={"agent": "claude-cli", "prompt": "x"})
        check(r.status_code == 403, "machine-only endpoints refuse a browser session")
        logs = d.get("/api/audit").json()["entries"]
        check(any(e["action"] == "login" and e["zone"] == "tunnel" for e in logs), "the login is in the activity log with its zone")
    # Cloudflare headers on the LAN port are treated as tunnel too (defence in depth)
    check(httpx.get(f"{d.url}/api/settings", headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 401, "Cloudflare headers on the main port still require a session")

    # File roots, including a symlink escape attempt
    (d.vault / "notes.md").write_text("hello vault")
    os.symlink("/etc/passwd", d.vault / "sneaky.md")
    check(d.get("/api/file", params={"path": str(d.vault / "notes.md")}).status_code == 200, "files in the vault are readable")
    check(d.get("/api/file", params={"path": str(d.cfg / "node_token")}).status_code == 403, "the token file is not readable through the API")
    check(d.get("/api/file", params={"path": str(d.vault / "sneaky.md")}).status_code == 403, "a symlink out of the vault is refused")
    check(d.get("/api/file", params={"path": "/etc/shadow"}).status_code == 403, "system files are refused")

    # Signed media links instead of ?token=
    img = d.data / "artifacts" / "chat1" / "pic.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG fake")
    q = d.get("/api/media/sign", params={"path": str(img)}).json()["query"]
    lan = httpx.get(f"{d.url}/api/media?{q}", headers={"Host": "192.0.2.10:8790"})
    check(lan.status_code == 200 and lan.content.startswith(b"\x89PNG"), "signed media link works without any token")
    tampered = q.replace("pic.png", "..%2F..%2F..%2Fcfg%2Fnode_token")
    check(httpx.get(f"{d.url}/api/media?{tampered}").status_code in (401, 403), "a tampered media link is refused")

    # One-time WebSocket tickets
    ticket = d.post("/api/ws/ticket").json()["ticket"]

    async def ws_try(url: str) -> bool:
        try:
            async with websockets.connect(url) as ws:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
                return True
        except asyncio.TimeoutError:
            return True  # connected; nothing to read yet
        except Exception:
            return False

    ws_url = f"ws://127.0.0.1:{d.port + 1}/ws/events?ticket={ticket}"
    check(asyncio.run(ws_try(ws_url)), "a ticket opens the event stream (even on the tunnel port)")
    check(not asyncio.run(ws_try(ws_url)), "the same ticket can't be used twice")
    check(not asyncio.run(ws_try(f"ws://127.0.0.1:{d.port + 1}/ws/events")), "the tunnel event stream needs a session or ticket")


# ---------------------------------------------------------------------------------------------------
def scenario_orchestrator(c: Cluster) -> None:
    print("\n== Orchestrator: background turns, approvals for model-chosen commands ==")
    d = c.desktop
    chat = d.post("/api/chats/native", {"title": "E2E orchestrator"}).json()
    marker = Path(tempfile.gettempdir()) / f"orch-e2e-{os.getpid()}"
    marker.unlink(missing_ok=True)

    async def run_turn_and_answer(content: str, approve: bool | None) -> tuple[dict, list]:
        ticket = d.post("/api/ws/ticket").json()["ticket"]
        seen: list[dict] = []
        async with websockets.connect(f"ws://127.0.0.1:{d.port}/ws/events?ticket={ticket}") as ws:
            t0 = time.time()
            res = d.post(f"/api/chats/native/{chat['id']}/send", {"content": content, "modelId": "fake-model"}).json()
            check(time.time() - t0 < 2.0 and bool(res.get("jobId")), "send returns at once with a job id (no long HTTP request)")
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                except asyncio.TimeoutError:
                    continue
                seen.append(ev)
                if ev["type"] == "approval_request" and approve is not None:
                    d.post(f"/api/approvals/{ev['requestId']}", {"approve": approve})
                if ev["type"] == "job_done" and ev["jobId"] == res["jobId"]:
                    break
        return d.wait_job(res["jobId"]), seen

    job, seen = asyncio.run(run_turn_and_answer(f"RUN: touch {marker}", approve=False))
    check(job["status"] == "done", "turn finished as a job")
    check(any(e["type"] == "approval_request" and "touch" in e["command"] for e in seen), "a non-read-only command asked for approval")
    check(not marker.exists(), "denied command did not run")
    msgs = d.get(f"/api/chats/desktop/orchestration/{chat['id']}").json()["messages"]
    check("Denied" in msgs[-1]["content"], "the model was told it was denied, and the reply was saved")

    job, seen = asyncio.run(run_turn_and_answer(f"RUN: touch {marker}", approve=True))
    check(marker.exists(), "approved command ran")
    marker.unlink(missing_ok=True)

    job, seen = asyncio.run(run_turn_and_answer("RUN: git status", approve=None))
    check(not any(e["type"] == "approval_request" for e in seen), "read-only `git status` ran without asking")
    check(any(e["type"] == "orch_progress" and "Running tool" in str(e.get("step")) for e in seen), "live progress was streamed")
    job, seen = asyncio.run(run_turn_and_answer("RUN: git status; rm -rf /", approve=False))
    check(any(e["type"] == "approval_request" for e in seen), "shell metacharacters make a 'read-only' command need approval")
    audit = d.get("/api/audit").json()["entries"]
    check(any(e["action"] == "approval" and e["detail"].get("approved") is False for e in audit), "denials are in the activity log")


# ---------------------------------------------------------------------------------------------------
def scenario_handoff(c: Cluster) -> None:
    print("\n== Repo sync: handoff of commits + uncommitted work, both directions, conflicts ==")
    d, l = c.desktop, c.laptop
    d_repo, l_repo = c.make_repo("webapp")
    d.get("/api/repos/inventory", params={"refresh": "true"})

    # You worked on the laptop: one unpushed commit, then more uncommitted edits + a new file
    (l_repo / "app.py").write_text("print('v2 from laptop')\n")
    git(l_repo, "commit", "-am", "laptop: v2")
    (l_repo / "app.py").write_text("print('v3 in progress')\n")
    (l_repo / "notes.txt").write_text("todo: finish v3\n")
    (l_repo / "debug.log").write_text("ignored\n")

    # Now you start Claude on the desktop in that repo: the SessionStart hook runs
    env = {**os.environ, "HOME": str(d.home), "LLM_ORCH_CONFIG_DIR": str(d.cfg)}
    hook = subprocess.run([str(ROOT / "scripts/hooks/orch-preflight.sh")], input=json.dumps({"cwd": str(d_repo)}), text=True,
                          capture_output=True, env=env, timeout=120)
    check("brought over work from laptop" in hook.stdout, f"hook reports the handoff: {hook.stdout.strip()[:160]}")
    check((d_repo / "app.py").read_text() == "print('v3 in progress')\n", "desktop has the laptop's uncommitted edit")
    check((d_repo / "notes.txt").read_text() == "todo: finish v3\n", "desktop has the laptop's new untracked file")
    check(not (d_repo / "debug.log").exists(), "gitignored files are not copied")
    check(git(d_repo, "log", "-1", "--format=%s") == "laptop: v2", "desktop fast-forwarded to the laptop's unpushed commit")
    check((l_repo / "app.py").read_text() == "print('v3 in progress')\n", "laptop's working copy was left untouched")
    remote_heads = git(c.origin / "webapp.git", "branch", "--list")
    check("wip/laptop/main" in remote_heads, "a wip/laptop/main copy was pushed to GitHub (origin)")
    check(git(c.origin / "webapp.git", "log", "-1", "--format=%s", "main") == "laptop: v2", "the unpushed commit was pushed to GitHub too")

    # Second preflight with nothing new: no-op
    res = d.post("/api/repos/preflight", {"cwd": str(d_repo)}).json()
    check(res["status"] in ("already_in_sync",), f"nothing new -> no handoff ({res['status']})")

    # Keep working on the desktop, then go back to the laptop (its old copy is untouched)
    (d_repo / "app.py").write_text("print('v3 done on desktop')\n")
    res = l.post("/api/repos/preflight", {"cwd": str(l_repo)}).json()
    check(res["status"] == "handed_off", f"handing back to the laptop works ({res.get('status')}: {res.get('message', '')[:120]})")
    check((l_repo / "app.py").read_text() == "print('v3 done on desktop')\n", "laptop now has the desktop's newer edit")
    check("orch-handoff-backup" in git(l_repo, "stash", "list"), "the laptop's old copy was stashed first (recoverable), not discarded")
    board = l.get("/api/repos/board").json()
    row = next(r for r in board["repos"] if r["repo"] == "webapp")
    check(row["active"] and row["active"]["nodeId"] == "laptop", "the sync board shows the laptop as the active device")

    # Real conflict: both machines edit before syncing -> nothing is overwritten
    (l_repo / "app.py").write_text("print('laptop edit A')\n")
    (d_repo / "app.py").write_text("print('desktop edit B')\n")
    res = d.post("/api/repos/preflight", {"cwd": str(d_repo)}).json()
    check(res["status"] == "conflict_both_dirty", f"both-dirty is reported as a conflict ({res['status']})")
    check((d_repo / "app.py").read_text() == "print('desktop edit B')\n" and (l_repo / "app.py").read_text() == "print('laptop edit A')\n",
          "neither machine's edit was overwritten")
    check("wip/desktop/main" in git(d_repo, "branch", "--list"), "the desktop's edit is also saved as a wip branch")
    handoffs = d.get("/api/repos/board").json()["handoffs"]
    check(any(h["status"] == "conflict_both_dirty" for h in handoffs), "the conflict is recorded in the handoff history")

    # Diverged commits are never merged automatically
    git(l_repo, "commit", "-am", "laptop: A")
    git(d_repo, "commit", "-am", "desktop: B")
    res = d.post("/api/repos/preflight", {"cwd": str(d_repo)}).json()
    check(res["status"] == "conflict_diverged", f"diverged branches stop with a report ({res['status']})")
    check(git(d_repo, "log", "-1", "--format=%s") == "desktop: B", "desktop's commit is untouched")


# ---------------------------------------------------------------------------------------------------
def scenario_replication(c: Cluster) -> None:
    print("\n== Same conversation on both machines; laptop works offline and merges back ==")
    d, l = c.desktop, c.laptop
    chat = d.post("/api/chats/native", {"title": "shared chat"}).json()
    job = d.post(f"/api/chats/native/{chat['id']}/send", {"content": "hello from desktop", "modelId": "fake-model"}).json()
    d.wait_job(job["jobId"])
    got = wait_until(lambda: len(l.get(f"/api/chats/laptop/orchestration/{chat['id']}").json().get("messages", [])) >= 2, timeout=20)
    check(bool(got), "the laptop has the desktop's conversation (replicated)")

    (d.vault / "plan.md").write_text("plan v1\n")
    check(bool(wait_until(lambda: (l.vault / "plan.md").is_file(), timeout=20)), "a vault file written on the desktop appears on the laptop")

    # Desktop goes offline; you keep talking to the laptop
    d.stop()
    job = l.post(f"/api/chats/native/{chat['id']}/send", {"content": "while desktop was off", "modelId": "fake-model", "nodeId": "desktop"}).json()
    j = l.wait_job(job["jobId"])
    msgs = l.get(f"/api/chats/laptop/orchestration/{chat['id']}").json()["messages"]
    check(j["status"] == "done" and "echo: while desktop was off" in msgs[-1]["content"], "laptop answered locally while the desktop was off")
    check("offline" in msgs[-1]["senderLabel"], f"the reply says it fell back and why ({msgs[-1]['senderLabel']})")
    (l.vault / "plan.md").write_text("plan v2 (laptop, offline)\n")
    time.sleep(1.1)
    (d.vault / "plan.md").write_text("plan v2 (desktop, offline)\n")  # edited on the desktop's disk while its server was down
    (l.vault / "only-laptop.md").write_text("new\n")

    d.start()
    ok = wait_until(lambda: len(d.get(f"/api/chats/desktop/orchestration/{chat['id']}").json()["messages"]) == len(msgs), timeout=30)
    check(bool(ok), "after reconnecting, the desktop has the laptop's offline messages")
    time.sleep(3)
    dm = d.get(f"/api/chats/desktop/orchestration/{chat['id']}").json()["messages"]
    lm = l.get(f"/api/chats/laptop/orchestration/{chat['id']}").json()["messages"]
    check([m["id"] for m in dm] == [m["id"] for m in lm], "both machines have the same messages, same order, no duplicates")

    def vault_converged() -> bool:
        names_d = sorted(p.name for p in d.vault.glob("plan*.md"))
        names_l = sorted(p.name for p in l.vault.glob("plan*.md"))
        return len(names_d) == 2 and names_d == names_l and (d.vault / "plan.md").read_text() == (l.vault / "plan.md").read_text()

    check(bool(wait_until(vault_converged, timeout=30)), "a vault file edited on both sides converges: newer wins, other kept as a conflict copy")
    if vault_converged():
        texts = sorted(p.read_text() for p in d.vault.glob("plan*.md"))
        check(texts == ["plan v2 (desktop, offline)\n", "plan v2 (laptop, offline)\n"], "both versions survive")
    check(bool(wait_until(lambda: (d.vault / "only-laptop.md").is_file(), timeout=20)), "files created offline on the laptop reach the desktop")
    (d.vault / "only-laptop.md").unlink()
    check(bool(wait_until(lambda: not (l.vault / "only-laptop.md").exists(), timeout=20)), "deleting a file on one machine deletes it on the other")


# ---------------------------------------------------------------------------------------------------
def scenario_agents(c: Cluster) -> None:
    print("\n== Agents: explicit target never runs silently elsewhere; paths map between machines ==")
    d, l = c.desktop, c.laptop
    l_repo = l.github / "webapp"
    r = l.post("/api/mcp/peer-ask", {"targetNode": "desktop", "agent": "claude-cli", "prompt": "hello", "cwd": str(l_repo)}, timeout=60)
    check(r.status_code == 200 and str(d.github / "webapp") in r.json()["response"], f"a laptop path runs in the desktop's copy of the repo ({r.json().get('response', '')[:100]})")
    d.stop()
    r = l.post("/api/mcp/peer-ask", {"targetNode": "desktop", "agent": "claude-cli", "prompt": "hello", "cwd": str(l_repo)}, timeout=60)
    check(r.status_code == 409 and "offline" in r.text, "asking for the desktop while it's off fails clearly instead of running on the laptop")
    r = l.post("/api/mcp/peer-ask", {"targetNode": "auto", "agent": "claude-cli", "prompt": "hello", "cwd": str(l_repo)}, timeout=60)
    check(r.status_code == 200 and r.json()["executedNode"] == "laptop", "'auto' runs locally")
    d.start()


# ---------------------------------------------------------------------------------------------------
def scenario_watchdog(c: Cluster) -> None:
    print("\n== Watchdog: threshold, backoff, give up + alert, recovery; backups to the other machine ==")
    d, l = c.desktop, c.laptop
    site = StatusServer(500)
    db = d.home / "site.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('precious')")
    con.commit()
    con.close()
    (d.state / "units").mkdir(parents=True, exist_ok=True)
    (d.state / "units" / "site-blue.service").write_text("failed")
    (d.state / "units" / "site-blue.service.sticky").write_text("")  # restarting doesn't fix it
    d.stop()
    c.desktop.write_settings(l, {"services": [
        {"id": "site", "name": "Test Site", "node": "desktop", "units": ["site-blue.service", "site-green.service"],
         "restartOrder": ["site-blue.service", "site-green.service"], "slotWaitSec": 1, "health": {"url": site.url}, "publicHost": "site.example",
         "backup": {"sqlite": [str(db)]}}],
        "backups": {"targetNode": "laptop", "intervalSec": 999999, "keep": 3}})
    d.start()

    def events() -> list[dict]:
        return d.get("/api/services/events").json()["events"]

    gave_up = wait_until(lambda: any(e["kind"] == "gave_up" for e in events()), timeout=60)
    ev = events()
    restarts = [e for e in ev if e["kind"] == "restarted"]
    check(bool(gave_up), "after repeated failures the watchdog gives up instead of looping")
    check(len(restarts) == 2, f"it restarted exactly maxRestartsPerHour=2 times ({len(restarts)})")
    log = (d.state / "systemctl.log").read_text().splitlines()
    order = [x for x in log if x.startswith("restart")]
    check(order[:2] == ["restart site-blue.service", "restart site-green.service"], f"blue/green restart one slot at a time ({order[:2]})")
    if len(restarts) == 2:
        gap = restarts[0]["ts"] - restarts[1]["ts"]
        check(gap >= 1.0, f"restarts back off ({gap:.1f}s apart)")
    status = next(s for s in d.get("/api/services").json()["nodes"]["desktop"] if s["id"] == "site")
    check(status["status"] == "gave_up", "the dashboard shows 'gave up'")
    alerts = [e for e in d.get("/api/audit").json()["entries"] if e["action"] == "alert"]
    check(any("stopped retrying" in e["target"] for e in alerts), "you were alerted that it stopped retrying")

    site.code = 200
    (d.state / "units" / "site-blue.service").write_text("active")
    check(bool(wait_until(lambda: any(e["kind"] == "recovered" for e in events()), timeout=20)), "recovery is noticed and reported")

    res = d.post("/api/backups/run").json()
    check(res["results"].get(f"site:{db.name}") == "sent", f"database snapshot was sent to the laptop ({res['results']})")
    files = list((l.home / "Backups" / "desktop" / "site").glob("*.gz"))
    check(len(files) >= 1, "the laptop stored it under Backups/desktop/site")
    if files:
        restored = l.home / "restored.db"
        restored.write_bytes(gzip.decompress(sorted(files)[-1].read_bytes()))
        val = sqlite3.connect(restored).execute("SELECT v FROM t").fetchone()[0]
        check(val == "precious", "the backup is a valid, restorable SQLite database")
    for _ in range(4):
        d.post("/api/backups/run")
    check(len(list((l.home / "Backups" / "desktop" / "site").glob("*.gz"))) <= 3, "old backups are pruned (keep=3)")
    site.server.shutdown()


# ---------------------------------------------------------------------------------------------------
def scenario_failover(c: Cluster) -> None:
    print("\n== Failover: the laptop takes over the sites while the desktop is down, then hands back ==")
    d, l = c.desktop, c.laptop
    internet = StatusServer(200)
    l.stop()
    c.laptop.write_settings(d, {"failover": {"enabled": True, "role": "standby", "primaryNode": "desktop", "downAfterSec": 2,
                                             "upAfterSec": 2, "connectorUnit": "failover-tunnel.service", "publicProbeUrl": "",
                                             "internetProbeUrl": internet.url}})
    l.start()
    log = l.state / "systemctl.log"
    time.sleep(5)
    check("start failover-tunnel.service" not in (log.read_text() if log.exists() else ""), "nothing happens while the desktop is up")
    d.stop()
    check(bool(wait_until(lambda: "start failover-tunnel.service" in log.read_text(), timeout=40)), "desktop down -> laptop starts the failover connector")
    check(l.get("/api/services").json()["failover"]["state"] == "active", "dashboard shows failover active")
    d.start()
    check(bool(wait_until(lambda: "stop failover-tunnel.service" in log.read_text(), timeout=40)), "desktop back -> laptop stops serving")
    internet.server.shutdown()


def scenario_sudo(c: Cluster) -> None:
    print("\n== Terminal sudo pop-up: the password goes from the UI to the askpass helper only ==")
    d = c.desktop

    async def go() -> tuple[dict, list]:
        ticket = d.post("/api/ws/ticket").json()["ticket"]
        seen: list = []
        async with websockets.connect(f"ws://127.0.0.1:{d.port}/ws/events?ticket={ticket}") as ws:
            want = __import__("hashlib").sha256(b"s3cret-pw").hexdigest()
            cmd = f'pw="$("$SUDO_ASKPASS" "[sudo] password for e2e:")" && [ "$(printf %s "$pw" | sha256sum | cut -d" " -f1)" = "{want}" ] && echo MATCHED'
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(None, lambda: d.post("/api/terminal/run", {"command": cmd, "cwd": str(d.home)}, timeout=60).json())
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                except asyncio.TimeoutError:
                    continue
                seen.append(ev)
                if ev["type"] == "password_prompt":
                    await loop.run_in_executor(None, lambda: d.post("/api/terminal/password", {"requestId": ev["requestId"], "password": "s3cret-pw"}))
                    break
            return await fut, seen

    res, seen = asyncio.run(go())
    check(any(e["type"] == "password_prompt" for e in seen), "the sudo prompt reached the UI as a pop-up event")
    check(res.get("exitCode") == 0 and "MATCHED" in res.get("output", ""), "the password reached the askpass helper")
    check("s3cret-pw" not in res.get("output", ""), "the password is not in the command output")
    db_text = (d.data / "orchestration.db").read_bytes()
    check(b"s3cret-pw" not in db_text, "the password was never written to the database")
    check(not any("s3cret-pw" in json.dumps(e) for e in seen), "the password never went out over the event stream")


def scenario_devices(c: Cluster) -> None:
    print("\n== Trusted devices: pairing, survives restarts, works on the standby, revocation reaches both ==")
    d, l = c.desktop, c.laptop
    token = d.post("/api/auth/pair/start").json()["token"]
    check(httpx.post(f"{d.tunnel}/api/auth/pair/start", headers={"X-Orch-Client": "1"}).status_code == 401, "strangers can't make pairing links")
    r = httpx.post(f"{d.tunnel}/api/auth/pair/redeem", json={"token": token}, headers={"X-Orch-Client": "1", "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"})
    cookie = r.headers.get("set-cookie", "").split(";", 1)[0]
    check(r.status_code == 200 and cookie.startswith("orch_session="), "a pairing link signs the phone in")
    check("Max-Age=2592000" in r.headers.get("set-cookie", ""), "the session cookie lasts 30 days")
    again = httpx.post(f"{d.tunnel}/api/auth/pair/redeem", json={"token": token}, headers={"X-Orch-Client": "1"})
    check(again.status_code == 401, "the pairing link works only once")
    phone = {"Cookie": cookie}
    check(httpx.get(f"{d.tunnel}/api/settings", headers=phone).status_code == 200, "the phone can use the app remotely")
    d.stop()
    d.start()
    check(httpx.get(f"{d.tunnel}/api/settings", headers=phone).status_code == 200, "still signed in after the desktop restarts")
    check(bool(wait_until(lambda: httpx.get(f"{l.tunnel}/api/settings", headers=phone).status_code == 200, timeout=30)),
          "the same phone session works on the laptop (failover keeps you signed in)")
    devs = d.get("/api/auth/devices").json()
    mine = next((s for s in devs["sessions"] if "iPhone" in s["name"]), None)
    check(bool(mine) and mine["method"] == "pairing", f"the devices list shows the phone ({mine and mine['name']})")
    check(d.post("/api/auth/devices/revoke", {"kind": "session", "id": mine["id"]}).status_code == 200 if mine else False, "the desktop signs the phone out")
    check(httpx.get(f"{d.tunnel}/api/settings", headers=phone).status_code == 401, "revoked on the desktop")
    check(bool(wait_until(lambda: httpx.get(f"{l.tunnel}/api/settings", headers=phone).status_code == 401, timeout=30)), "revocation reaches the laptop too")
    log = [e["action"] for e in d.get("/api/audit").json()["entries"]]
    check("pairing_link" in log and "device_revoked" in log, "pairing and revocation are in the activity log")


def scenario_agent_orchestrator(c: Cluster) -> None:
    print("\n== Claude as the orchestrator: briefing, own session per chat, shared tools via the MCP bridge ==")
    d = c.desktop
    chat = d.post("/api/chats/native", {"title": "Claude orchestrates"}).json()
    log = d.state / "claude.log"
    log.unlink(missing_ok=True)
    for text in ("first message", "second message"):
        job = d.post(f"/api/chats/native/{chat['id']}/send", {"content": text, "modelId": "claude-cli", "nodeId": "desktop"}).json()
        d.wait_job(job["jobId"])
    runs = [json.loads(x) for x in log.read_text().splitlines()]
    check(len(runs) == 2, f"two Claude turns ran ({len(runs)})")
    argv0 = runs[0]["argv"]
    brief = argv0[argv0.index("--append-system-prompt") + 1] if "--append-system-prompt" in argv0 else ""
    check("orch_repo_preflight" in brief and "Configured Task Routing" in brief and "Never invent results" in brief,
          "Claude gets the orchestrator briefing (routing table, preflight rule, no-guessing rule)")
    check("--mcp-config" in argv0 and argv0[1] == "first message", "Claude gets the MCP bridge and the prompt in the right place")
    check("--resume" in runs[1]["argv"] and runs[1]["argv"][runs[1]["argv"].index("--resume") + 1] == "fake-session-1",
          "the second turn continues the same Claude session")

    env = {**os.environ, "LLM_ORCH_CONFIG_DIR": str(d.cfg), "LLM_ORCH_URL": d.url, "HOME": str(d.home)}
    bridge = subprocess.Popen([sys.executable, str(ROOT / "server" / "mcp_bridge.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    def rpc(i: int, method: str, params: dict | None = None) -> dict:
        bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        bridge.stdin.flush()
        return json.loads(bridge.stdout.readline())

    try:
        names = {t["name"] for t in rpc(1, "tools/list")["result"]["tools"]}
        check({"orch_run_specialist_llm", "orch_sync_repo", "orch_run_command", "orch_repo_preflight", "orch_peer_ask"} <= names,
              "the bridge offers the orchestrator tools plus its own")
        settings_out = json.loads(rpc(2, "tools/call", {"name": "orch_get_settings", "arguments": {}})["result"]["content"][0]["text"])
        check("coding" in settings_out.get("taskDefaults", {}), "Claude can read the task routing table through the bridge")

        marker = d.home / "bridge-marker"

        async def deny_next_approval() -> bool:
            ticket = d.post("/api/ws/ticket").json()["ticket"]
            async with websockets.connect(f"ws://127.0.0.1:{d.port}/ws/events?ticket={ticket}") as ws:
                deadline = time.time() + 20
                while time.time() < deadline:
                    try:
                        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                    except asyncio.TimeoutError:
                        continue
                    if ev["type"] == "approval_request":
                        d.post(f"/api/approvals/{ev['requestId']}", {"approve": False})
                        return True
            return False

        import threading
        seen: list[bool] = []
        t = threading.Thread(target=lambda: seen.append(asyncio.run(deny_next_approval())))
        t.start()
        time.sleep(0.5)
        out = json.loads(rpc(3, "tools/call", {"name": "orch_run_command", "arguments": {"command": f"touch {marker}", "targetNode": "laptop"}})["result"]["content"][0]["text"])
        t.join(timeout=25)
        check(seen == [True], "a command Claude wants to run on the other machine raises your approval card")
        check("Denied" in str(out.get("error")) and not marker.exists(), "denying it really blocks it")
    finally:
        bridge.kill()


def scenario_usage_handoff(c: Cluster) -> None:
    print("\n== Local router; Claude -> AGY -> local model when agents run out of usage ==")
    d = c.desktop
    if not (d.github / "usagedemo").exists():
        c.make_repo("usagedemo")
    d.get("/api/repos/inventory", params={"refresh": "true"})
    chat = d.post("/api/chats/native", {"title": "Usage handoff"}).json()
    claude_log, agy_log = d.state / "claude.log", d.state / "agy.log"
    for f in (claude_log, agy_log, d.state / "claude.limit", d.state / "agy.limit"):
        f.unlink(missing_ok=True)

    def turn(text: str) -> dict:
        job = d.post(f"/api/chats/native/{chat['id']}/send", {"content": text, "modelId": "fake-model", "nodeId": "desktop"}).json()
        d.wait_job(job["jobId"], timeout=60)
        return d.get(f"/api/chats/desktop/orchestration/{chat['id']}").json()["messages"][-1]

    def lines(f: Path) -> list[dict]:
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    m = turn("please fix the bug in the usagedemo repo")
    check(m["senderLabel"].startswith("Claude (desktop) · coding"), f"a code request goes to Claude first ({m['senderLabel']})")
    check(lines(claude_log)[-1]["cwd"].endswith("/usagedemo"), "Claude works inside the repo the request is about")

    (d.state / "claude.limit").write_text("")
    m = turn("review the usagedemo code again")
    check(m["senderLabel"].startswith("AGY (desktop) · coding") and "Claude out of usage until" in m["senderLabel"],
          f"Claude out of usage -> AGY takes over, and the reply says why ({m['senderLabel']})")
    agy_prompt = lines(agy_log)[-1]["prompt"]
    check("Claude was handling this" in agy_prompt and "please fix the bug" in agy_prompt, "AGY is handed the conversation so far")
    status = d.get("/api/status").json()
    check("claude-cli" in status["agentsOut"], "the status strip knows Claude is out")

    calls = len(lines(claude_log))
    m = turn("one more code review please")
    check(len(lines(claude_log)) == calls and m["senderLabel"].startswith("AGY"), "while Claude is out it isn't even tried")

    (d.state / "agy.limit").write_text("")
    m = turn("fix another bug in usagedemo")
    label = m["senderLabel"]
    check(label.startswith("Agent (desktop)") and "AGY out of usage" in label and "answered by local" in label,
          f"both out -> a local model answers, and says so ({label})")

    calls_c, calls_a = len(lines(claude_log)), len(lines(agy_log))
    m = turn("write a short story about a lighthouse")
    check(len(lines(claude_log)) == calls_c and len(lines(agy_log)) == calls_a and "echo:" in m["content"],
          "story writing stays on the local model (no paid agent)")
    m = turn("hello there")
    check(len(lines(claude_log)) == calls_c and "echo: hello there" in m["content"], "small talk stays on the free local orchestrator")
    (d.state / "agy.limit").unlink()
    (d.state / "claude.limit").unlink()
    m = d.post(f"/api/chats/native/{chat['id']}/send", {"content": "review usagedemo", "modelId": "claude-cli", "nodeId": "desktop"}).json()
    d.wait_job(m["jobId"], timeout=60)
    m = d.get(f"/api/chats/desktop/orchestration/{chat['id']}").json()["messages"][-1]
    check(m["senderLabel"].startswith("Claude") and "claude-cli" not in d.get("/api/status").json()["agentsOut"],
          "picking Claude yourself tries it even while marked out; success clears the mark")


SCENARIOS = {"usage_handoff": scenario_usage_handoff, "agent_orchestrator": scenario_agent_orchestrator, "devices": scenario_devices, "sudo": scenario_sudo, "security": scenario_security, "orchestrator": scenario_orchestrator, "handoff": scenario_handoff,
             "replication": scenario_replication, "agents": scenario_agents, "watchdog": scenario_watchdog, "failover": scenario_failover}


def main() -> int:
    wanted = sys.argv[1:] or list(SCENARIOS)
    c = Cluster(keep="--keep" in wanted)
    wanted = [w for w in wanted if w != "--keep"]
    c.configure()
    if "agents" in wanted and "handoff" not in wanted:
        c.make_repo("webapp")
    c.start()
    try:
        for name in wanted:
            try:
                SCENARIOS[name](c)
            except Exception as exc:  # a crashed scenario is a failure, not a stop
                import traceback

                traceback.print_exc()
                check(False, f"{name} crashed: {exc}")
    finally:
        c.stop()
    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
