#!/usr/bin/env python3
"""End-to-end: the orchestrator card's three terminals (Claude, AGY, local) in real tmux.

Real server process, real tmux, real git repo with a bare "GitHub" origin; fake claude/agy/opencode
TUIs on PATH that behave like the real ones (prompt, line in, answer out, Claude's hooks). Every
check looks at what actually happened: what reached which terminal, what git refused, what the
vault holds.

Run:  ${ORCH_PYTHON:-~/.local/share/llm-orchestration/venv/bin/python} e2e/test_terminal_orchestrator.py [--keep]
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, TOTP_SECRET, Cluster, check, failures, git, wait_until  # noqa: E402

sys.path.insert(0, str(ROOT))
from server.auth import _hotp  # noqa: E402

API = "/api/orch-terminal"


def totp_now() -> str:
    return _hotp(TOTP_SECRET, int(time.time() // 30))


def lines(node, agent: str) -> list[str]:
    f = node.state / f"{agent}.lines"
    return f.read_text().splitlines() if f.exists() else []


def tui_launches(node, agent: str) -> list[dict[str, Any]]:
    f = node.state / f"{agent}-tui.log"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def card(node, cid: str) -> dict[str, Any]:
    return next(c for c in node.get(f"{API}/cards").json()["cards"] if c["id"] == cid)


def slot(node, cid: str, name: str) -> dict[str, Any]:
    return card(node, cid)["slots"][name]


def tmux_has(node, session: str) -> bool:
    sock = node.data / "runtime" / "orch-tmux.sock"
    return subprocess.run(["tmux", "-S", str(sock), "has-session", "-t", session], capture_output=True).returncode == 0


class Term:
    """A browser-like client of /ws/orch-terminal: JSON frames up, raw screen bytes down."""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.screen = ""

    async def __aenter__(self) -> "Term":
        self.ws = await websockets.connect(self.url, max_size=None)
        await self.ws.send(json.dumps({"t": "r", "c": 120, "r": 30}))
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ws.close()

    async def read_until(self, needle: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while needle not in self.screen and time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=max(0.05, deadline - time.time()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            self.screen += msg.decode("utf-8", "replace") if isinstance(msg, bytes) else msg
        return needle in self.screen or needle in self.text()

    def text(self) -> str:
        """Roughly what a terminal shows: tmux draws runs of spaces as cursor moves (ESC[nC)."""
        t = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), self.screen)
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\x1b[=>]", "", t)

    async def type(self, text: str) -> None:
        await self.ws.send(json.dumps({"t": "i", "d": text}))


def ticket(node, base: str | None = None, client: httpx.Client | None = None) -> str:
    if client is not None:
        return client.post("/api/ws/ticket", headers={"X-Orch-Client": "1"}).json()["ticket"]
    return node.post("/api/ws/ticket").json()["ticket"]


def term_url(node, cid: str, name: str, t: str, tunnel: bool = False) -> str:
    port = node.port + 1 if tunnel else node.port
    return f"ws://127.0.0.1:{port}/ws/orch-terminal/{cid}/{name}?ticket={t}"


async def refused(url: str) -> bool:
    try:
        async with websockets.connect(url) as ws:
            await asyncio.wait_for(ws.recv(), timeout=1.5)
            return False
    except asyncio.TimeoutError:
        return False
    except Exception:
        return True


# ---------------------------------------------------------------------------------------------------
def scenario_terminal_orchestrator(c: Cluster, repo: Path) -> None:
    d = c.desktop
    print("\n== card setup: branch, context file, Claude terminal ==")
    r = d.post(f"{API}/cards", {"repo": "webapp"})
    check(r.status_code == 200, f"a card is created for the webapp repo ({r.status_code} {r.text[:200]})")
    cd = r.json()
    cid = cd["id"]
    check(git(repo, "branch", "--show-current") == "orchestrator-modified", "the repo is switched to orchestrator-modified")
    ctx_file = repo / ".orch" / "context.md"
    text = ctx_file.read_text() if ctx_file.exists() else ""
    check(all(h in text for h in ("## Goal", "## Branch", "## Done so far", "## Decisions", "## Next steps", "## Handoff log")),
          "the context file has the fixed sections")
    check(cd["holder"] == "claude", "Claude holds the baton first")
    check(bool(wait_until(lambda: tui_launches(d, "claude"), timeout=10)), "Claude was started in its terminal")
    launch = tui_launches(d, "claude")[0]
    argv = launch["argv"]
    check("--dangerously-skip-permissions" in argv and "--remote-control" in argv and "--session-id" in argv,
          f"Claude runs with skip-permissions, Remote Control and a fixed session id ({argv[:8]})")
    check("--mcp-config" in argv and "--append-system-prompt-file" in argv and "--settings" in argv,
          "Claude gets the orch tools, the briefing and the busy/idle hooks")
    check(launch["cwd"] == str(repo), "Claude runs in the repo")
    check(launch["env"].get("GIT_CONFIG_KEY_0") == "core.hooksPath", "Claude's git uses the orchestrator's branch hooks")
    check(d.post(f"{API}/cards", {"repo": "webapp"}).json()["id"] == cid, "asking again for the same repo returns the same card")
    check(d.post(f"{API}/cards", {"repo": "../../etc"}).status_code in (400, 404), "a path outside the GitHub folder is refused")

    t1, t2 = ticket(d), ticket(d)

    async def browser_session() -> None:
        async with Term(term_url(d, cid, "claude", t1)) as a, Term(term_url(d, cid, "claude", t2)) as b:
            # (the start banner may have scrolled up when tmux rewrapped to this width; the prompt is on screen)
            check(await a.read_until("claude>"), f"the browser terminal shows Claude's screen ({a.text()[-300:]!r})")
            await a.type("ECHO hello-from-browser\r")
            check(await a.read_until("hello-from-browser\r\n"), "typing in the browser reaches Claude")
            check(await b.read_until("hello-from-browser"), "a second browser watching the same terminal sees it too")
            await a.type("SLOW 3\r")
            check(bool(wait_until(lambda: slot(d, cid, "claude")["busy"], timeout=3)), "Claude's hook marks it busy while it works")
            check(bool(wait_until(lambda: not slot(d, cid, "claude")["busy"], timeout=8)), "Claude's Stop hook marks it idle again")
    asyncio.run(browser_session())

    print("\n== the baton: only the holder takes input ==")
    check(d.post(f"{API}/cards/{cid}/slots/agy/start").status_code == 200, "the AGY terminal can be started")
    check(bool(wait_until(lambda: tui_launches(d, "agy"), timeout=10)), "AGY is running in its terminal")
    check("--dangerously-skip-permissions" in tui_launches(d, "agy")[0]["argv"], "AGY runs with skip-permissions")

    async def not_holder() -> None:
        async with Term(term_url(d, cid, "agy", ticket(d))) as t:
            check(await t.read_until("fake-agy started"), "AGY's screen can be watched")
            await t.type("ECHO sneaky-agy-input\r")
            await asyncio.sleep(1.0)
    asyncio.run(not_holder())
    check(not any("sneaky-agy-input" in x for x in lines(d, "agy")), "keystrokes to a terminal without the baton are dropped")
    r = d.post(f"{API}/cards/{cid}/send", {"text": "ECHO via-phone-box"})
    check(r.status_code == 200 and bool(wait_until(lambda: "ECHO via-phone-box" in lines(d, "claude"), timeout=5)),
          "the phone input box types into the baton holder")

    print("\n== branch-only work ==")
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ntouch \"$(git rev-parse --git-dir)/repo-own-hook-ran\"\n")
    (repo / ".git" / "hooks" / "pre-commit").chmod(0o755)
    d.post(f"{API}/cards/{cid}/send", {"text": "COMMIT work on the branch"})
    check(bool(wait_until(lambda: git(repo, "log", "-1", "--format=%s", "orchestrator-modified") == "work on the branch", timeout=8)),
          "an agent can commit on orchestrator-modified")
    check((repo / ".git" / "repo-own-hook-ran").exists(), "the repo's own pre-commit hook still runs")
    main_before = git(repo, "rev-parse", "main")
    d.post(f"{API}/cards/{cid}/send", {"text": "COMMIT_ON main sneaky commit on main"})
    time.sleep(3)
    check(git(repo, "rev-parse", "main") == main_before, "an agent's commit on main is refused")
    check(git(repo, "branch", "--show-current") == "orchestrator-modified", "the repo is back on the working branch")
    git(repo, "stash", "-u")  # the refused commit's file
    git(repo, "checkout", "-q", "main")
    (repo / "mine.txt").write_text("mine\n")
    git(repo, "add", "mine.txt")
    git(repo, "commit", "-q", "-m", "Todd commits on main himself")
    check(git(repo, "log", "-1", "--format=%s", "main") == "Todd commits on main himself", "your own terminals can still commit on main")
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    git(repo, "checkout", "-q", "orchestrator-modified")
    git(repo, "stash", "drop", "-q")

    print("\n== out of usage, then a handoff ==")
    d.post(f"{API}/cards/{cid}/send", {"text": "LIMIT"})
    check(bool(wait_until(lambda: slot(d, cid, "claude")["outUntil"] > time.time(), timeout=8)), "Claude's usage-limit message marks it out")
    check(card(d, cid)["holder"] == "claude", "running out doesn't move the baton by itself")
    r = d.post(f"{API}/cards/{cid}/handoff", {"to": "agy"})
    check(r.status_code == 202, f"you can hand the baton to AGY ({r.status_code} {r.text[:160]})")
    check(bool(wait_until(lambda: card(d, cid)["holder"] == "agy", timeout=15)), "AGY holds the baton")
    check(bool(wait_until(lambda: any(x.startswith("Taking over from Claude") for x in lines(d, "agy")), timeout=8)),
          "the handoff message was typed into AGY's terminal")
    log = ctx_file.read_text()
    check("Claude → AGY" in log and "out of usage" in log, "the handoff is in the context file's log, with the reason")
    vault_copy = d.vault / "orchestrator" / "webapp" / "context.md"
    check(vault_copy.exists() and vault_copy.read_text() == log, "a read-only copy of the context is in the vault")

    print("\n== no handoff while the holder is busy; interrupt; update before handing over ==")
    d.post(f"{API}/cards/{cid}/slots/local/start")
    check(bool(wait_until(lambda: tui_launches(d, "opencode"), timeout=10)), "the local terminal runs opencode")
    oc = tui_launches(d, "opencode")[0]
    check("--auto" in oc["argv"] and oc["env"].get("OPENCODE_CONFIG", "").endswith(".json"),
          "opencode uses its own config and auto-approve")
    oc_cfg = json.loads(Path(oc["env"]["OPENCODE_CONFIG"]).read_text())
    check(oc_cfg.get("model") == "local/qwen3-8b", "opencode's config picks the configured local model")
    check(c.llm.base in json.dumps(oc_cfg), "opencode's config points at this machine's local model server")
    d.post(f"{API}/cards/{cid}/send", {"text": "SLOW 30"})
    check(bool(wait_until(lambda: slot(d, cid, "agy")["busy"], timeout=6)), "AGY counts as busy while its screen keeps changing")
    r = d.post(f"{API}/cards/{cid}/handoff", {"to": "local"})
    check(r.status_code == 409, f"handing off while AGY is busy is refused ({r.status_code})")
    d.post(f"{API}/cards/{cid}/interrupt")
    check(bool(wait_until(lambda: not slot(d, cid, "agy")["busy"], timeout=12)), "Ctrl-C from the card stops AGY and it goes idle")
    r = d.post(f"{API}/cards/{cid}/handoff", {"to": "local"})
    check(r.status_code == 202, "once AGY is idle the handoff starts")
    check(bool(wait_until(lambda: any("Update .orch/context.md" in x for x in lines(d, "agy")), timeout=8)),
          "AGY was asked to update the context file first")
    check(bool(wait_until(lambda: card(d, cid)["holder"] == "local", timeout=20)), "then the baton moves to the local agent")
    check("agy updated the context before handing off" in ctx_file.read_text(), "AGY's update is in the context file")
    check(bool(wait_until(lambda: any(x.startswith("Taking over from AGY") for x in lines(d, "opencode")), timeout=8)),
          "the local agent got the handoff message")

    print("\n== server restart mid-task ==")
    d.post(f"{API}/cards/{cid}/send", {"text": "ECHO before-restart-marker"})
    wait_until(lambda: "ECHO before-restart-marker" in lines(d, "opencode"), timeout=5)
    d.stop()
    d.start()
    check(tmux_has(d, f"orch-{cid}-local"), "the terminals survive a server restart")
    check(card(d, cid)["holder"] == "local", "the baton is remembered across the restart")

    async def after_restart() -> None:
        async with Term(term_url(d, cid, "local", ticket(d))) as t:
            check(await t.read_until("before-restart-marker"), "reopening shows the same screen as before the restart")
    asyncio.run(after_restart())

    print("\n== over the internet: watch only ==")
    web = httpx.Client(base_url=d.tunnel, timeout=20)
    login = web.post("/api/auth/login", json={"code": totp_now()}, headers={"X-Orch-Client": "1"})
    # The session cookie is Secure (the real site is HTTPS), so this plain-HTTP test client sends it by hand.
    web.headers["Cookie"] = f"orch_session={login.cookies.get('orch_session', '')}"
    web.cookies.clear()
    r = web.get(f"{API}/cards")
    check(r.status_code == 200, f"the cards can be read over the tunnel (login {login.status_code}, cards {r.status_code} {r.text[:120]})")
    check(web.post(f"{API}/cards/{cid}/handoff", json={"to": "claude"}, headers={"X-Orch-Client": "1"}).status_code == 403,
          "a handoff can't be started over the tunnel")
    check(web.post(f"{API}/cards/{cid}/send", json={"text": "ECHO from-internet"}, headers={"X-Orch-Client": "1"}).status_code == 403,
          "the input box doesn't type over the tunnel")
    tt = ticket(d, client=web)

    async def tunnel_watch() -> None:
        async with Term(term_url(d, cid, "local", tt, tunnel=True)) as t:
            check(await t.read_until("before-restart-marker"), "the terminal can be watched over the tunnel")
            await t.type("ECHO typed-over-tunnel\r")
            await asyncio.sleep(1.0)
    asyncio.run(tunnel_watch())
    check(not any("typed-over-tunnel" in x for x in lines(d, "opencode")), "typing over the tunnel never reaches the terminal")
    check(asyncio.run(refused(term_url(d, cid, "local", tt, tunnel=True))), "a ticket can't be used twice")
    check(asyncio.run(refused(term_url(d, cid, "local", "forged-ticket", tunnel=True))), "a forged ticket is refused")
    check(asyncio.run(refused(term_url(d, cid, "nonsense", ticket(d)))), "an unknown terminal name is refused")
    conn = [e for e in d.get("/api/audit").json()["entries"] if e["action"] == "terminal_connect"]
    check(any(e["zone"] == "tunnel" and e["detail"].get("input") is False for e in conn), "a tunnel connection is logged as watch-only")
    check(any(e["zone"] == "local" and e["detail"].get("input") is True for e in conn), "a local connection is logged with input")

    print("\n== Claude exits, then resume ==")
    r = d.post(f"{API}/cards/{cid}/handoff", {"to": "claude"})
    check(r.status_code == 202, f"handing back to Claude is accepted ({r.status_code} {r.text[:200]})")
    check(bool(wait_until(lambda: card(d, cid)["holder"] == "claude", timeout=20)), "the baton can go back to Claude")
    sid = argv[argv.index("--session-id") + 1]
    d.post(f"{API}/cards/{cid}/send", {"text": "/exit"})
    check(bool(wait_until(lambda: slot(d, cid, "claude")["state"] == "exited", timeout=8)), "the card notices Claude exited")
    check(d.post(f"{API}/cards/{cid}/slots/claude/resume").status_code == 200, "Resume restarts Claude")
    check(bool(wait_until(lambda: len(tui_launches(d, "claude")) == 2, timeout=10)), "Claude was relaunched")
    re_argv = tui_launches(d, "claude")[-1]["argv"]
    check("--resume" in re_argv and re_argv[re_argv.index("--resume") + 1] == sid, "Resume continues the same Claude conversation")

    print("\n== merge to main ==")
    branch_head = git(repo, "rev-parse", "orchestrator-modified")
    r = d.post(f"{API}/cards/{cid}/merge")
    check(r.status_code == 200, f"merge succeeds ({r.status_code} {r.text[:200]})")
    check(git(repo, "merge-base", "--is-ancestor", branch_head, "main") == "" , "main contains the branch's work")
    check(".orch/context.md" not in git(repo, "ls-tree", "-r", "--name-only", "main"), "the context file stays out of main")
    check(git(c.origin / "webapp.git", "rev-parse", "main") == git(repo, "rev-parse", "main"), "main was pushed to GitHub")
    check(git(repo, "rev-parse", "orchestrator-modified") == git(repo, "rev-parse", "main"), "the branch was recreated from the new main")
    check(git(repo, "branch", "--show-current") == "orchestrator-modified", "the repo is on the fresh branch")
    fresh = ctx_file.read_text() if ctx_file.exists() else ""
    check("## Goal" in fresh and "agy updated the context" not in fresh, "the fresh branch starts with a clean context file")
    archived = list((d.vault / "orchestrator" / "webapp" / "archive").glob("*-context.md"))
    check(len(archived) == 1 and "agy updated the context" in archived[0].read_text(), "the old context is archived in the vault")


def main() -> int:
    if not shutil.which("tmux"):
        print("tmux is not installed: sudo pacman -S tmux")
        return 1
    keep = "--keep" in sys.argv
    c = Cluster(keep=keep)
    # The fakes stop on Ctrl-C; the real AGY and opencode take Escape (the default).
    c.configure(desktop_extra={"orchTerminal": {"detachWithSystemd": False, "localModel": "qwen3-8b",
                                                "interruptKeys": {"agy": ["C-c"], "local": ["C-c"]}}})
    d_repo, _ = c.make_repo("webapp")
    c.desktop.start()
    try:
        scenario_terminal_orchestrator(c, d_repo)
    except Exception as exc:  # a crash is a failure with its traceback, not a stop
        import traceback

        traceback.print_exc()
        check(False, f"crashed: {exc}")
    finally:
        c.stop()
    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
