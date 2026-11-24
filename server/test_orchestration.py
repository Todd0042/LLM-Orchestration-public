"""Automated tests for LLM-Orchestration Hub/Worker daemon, TOTP auth, Repos/Tools DB, Vault, Intermission Catch-Up & CLI agents."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import app as orch_app  # noqa: E402
from server import auth as orch_auth  # noqa: E402
from server import cli_agents  # noqa: E402
from server import config as orch_cfg  # noqa: E402
from server import history_readers  # noqa: E402
from server import inventory_sync  # noqa: E402
from server import orch_tools  # noqa: E402


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg_dir = root / "cfg"
        self.data_dir = root / "data"
        self.db_path = root / "orch.db"
        self.dist_dir = root / "dist"
        self.vault_dir = root / "vault"
        self.gh_dir = root / "GitHub"
        self.gh_dir.mkdir(parents=True)
        self.dist_dir.mkdir(parents=True)
        (self.dist_dir / "index.html").write_text("<html><body>OK</body></html>", encoding="utf-8")

        # Create a test TOTP secret so tunnel auth tests do not touch user state
        self.totp_dir = root / "totp"
        self.totp_dir.mkdir(parents=True)
        self.totp_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        (self.totp_dir / "totp_secret").write_text(self.totp_secret + "\n", encoding="utf-8")

        # Create sample git repo inside temporary GitHub dir
        repo = self.gh_dir / "demo-app"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True, check=True)
        (repo / "README.md").write_text("# Demo App\nCollects no PII.", encoding="utf-8")
        (repo / "privacy.md").write_text("Privacy Notice", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

        # Seed isolated settings
        self.cfg_dir.mkdir(parents=True)
        settings = orch_cfg.default_settings()
        settings["node"]["id"] = "laptop"
        settings["node"]["role"] = "hub"
        settings["paths"]["vaultDir"] = str(self.vault_dir)
        settings["paths"]["githubDir"] = str(self.gh_dir)
        settings["auth"]["totpStateDir"] = str(self.totp_dir)
        orch_cfg.save_settings(settings, self.cfg_dir / "settings.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_real_agy_and_claude_chat_readers(self) -> None:
        agy_dir = Path.home() / ".gemini" / "antigravity-cli"
        claude_dir = Path.home() / ".claude"
        agy_chats = history_readers.list_agy_chats(agy_dir, "laptop", limit=5)
        self.assertGreater(len(agy_chats), 0, "Expected AGY chats on host")
        detail = history_readers.read_agy_chat(agy_dir, agy_chats[0]["id"], "laptop")
        self.assertIsNotNone(detail)
        self.assertGreater(len(detail["messages"]), 0)

        claude_chats = history_readers.list_claude_chats(claude_dir, "laptop", limit=5)
        self.assertGreater(len(claude_chats), 0, "Expected Claude chats on host")
        cdetail = history_readers.read_claude_chat(claude_dir, claude_chats[0]["id"], "laptop")
        self.assertIsNotNone(cdetail)
        self.assertGreater(len(cdetail["messages"]), 0)



    def test_agy_and_claude_cli_flags_and_ndjson_parsers(self) -> None:
        agy_cmd = cli_agents.build_agy_command(
            "agy", "Fix bug", conversation_id="conv-123", model="gemini-pro-orchestrator", effort="xhigh", mode="plan"
        )
        self.assertIn("--dangerously-skip-permissions", agy_cmd)
        self.assertIn("--output-format=stream-json", agy_cmd)
        self.assertIn("-p=Fix bug", agy_cmd)
        self.assertIn("--conversation=conv-123", agy_cmd)
        self.assertIn("--model=gemini-pro-orchestrator", agy_cmd)
        self.assertIn("--effort=xhigh", agy_cmd)
        self.assertIn("--mode=plan", agy_cmd)

        # Tier-suffixed AGY models must never emit a conflicting --effort flag
        agy_conflict_safe = cli_agents.build_agy_command(
            "agy", "List repos", model="gemini-3.8-flash-medium", effort="high", mode="accept-edits"
        )
        self.assertIn("--model=gemini-3.8-flash-medium", agy_conflict_safe)
        self.assertFalse(any(a.startswith("--effort=") for a in agy_conflict_safe))

        claude_cmd = cli_agents.build_claude_command(
            "claude", "Fix bug", session_id="sess-456", model="opus", fallback_model="sonnet", effort="max"
        )
        self.assertIn("--dangerously-skip-permissions", claude_cmd)
        self.assertIn("--verbose", claude_cmd)
        self.assertIn("stream-json", claude_cmd)
        self.assertEqual(claude_cmd[claude_cmd.index("--resume") + 1], "sess-456")
        self.assertEqual(claude_cmd[claude_cmd.index("--fallback-model") + 1], "sonnet")
        self.assertEqual(claude_cmd[claude_cmd.index("--effort") + 1], "max")
        self.assertEqual(claude_cmd[1:3], ["-p", "Fix bug"])  # prompt must precede --mcp-config (it takes several values)

        mock_agy = Path(self.tmp.name) / "mock_agy.py"
        mock_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'event': 'init', 'conversation_id': 'c-99'}))\n"
            "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'Hello '}}))\n"
            "print(json.dumps({'event': 'result', 'result': {'conversation_id': 'c-99', 'response': 'Hello AGY'}}))\n",
            encoding="utf-8",
        )
        os.chmod(mock_agy, 0o700)

        mock_claude = Path(self.tmp.name) / "mock_claude.py"
        mock_claude.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'type': 'system', 'session_id': 's-88', 'model': 'claude-opus-test'}))\n"
            "print(json.dumps({'type': 'assistant', 'session_id': 's-88', 'message': {'model': 'claude-opus-test', 'content': [{'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': '/tmp/a.py', 'old_string': 'x', 'new_string': 'y'}}, {'type': 'text', 'text': 'Done Claude'}]}}))\n",
            encoding="utf-8",
        )
        os.chmod(mock_claude, 0o700)

        agy_res = asyncio.run(cli_agents.run_agy_turn("hi", binary_override=str(mock_agy)))
        self.assertEqual(agy_res["sessionId"], "c-99")
        self.assertEqual(agy_res["response"], "Hello AGY")

        cl_res = asyncio.run(cli_agents.run_claude_turn("hi", binary_override=str(mock_claude)))
        self.assertEqual(cl_res["sessionId"], "s-88")
        self.assertEqual(cl_res["response"], "Done Claude")
        self.assertEqual(cl_res["events"][0]["type"], "diff")

    def test_mcp_stdio_bridge_protocol(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "server" / "mcp_bridge.py")],
            input=(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
            ),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines() if l.strip()]
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "llm-orchestration-bridge")
        tool_names = {t["name"] for t in lines[1]["result"]["tools"]}
        self.assertIn("orch_peer_ask", tool_names)
        self.assertIn("orch_repo_inventory", tool_names)
        self.assertIn("orch_vault_write", tool_names)

    def test_intermission_catchup_sync_when_desktop_reconnects(self) -> None:
        conn = orch_cfg.init_db(self.db_path)
        try:
            eid1 = inventory_sync.enqueue_intermission_event(conn, "vault_change", "plans/offline.md", {})
            eid2 = inventory_sync.enqueue_intermission_event(conn, "repo_commit", "missing-repo", {})
            self.assertGreater(eid2, eid1)

            # Start a real temporary Desktop Worker FastAPI instance in background thread
            desk_gh = Path(self.tmp.name) / "DeskGitHub"
            desk_gh.mkdir(parents=True)
            desk_cfg_dir = Path(self.tmp.name) / "desk_cfg"
            desk_cfg_dir.mkdir(parents=True)
            desk_s = orch_cfg.default_settings()
            desk_s["node"]["id"] = "desktop"
            desk_s["node"]["role"] = "worker"
            desk_s["paths"]["githubDir"] = str(desk_gh)
            orch_cfg.save_settings(desk_s, desk_cfg_dir / "settings.json")
            desk_app = orch_app.create_app(
                config_dir=desk_cfg_dir,
                db_path=Path(self.tmp.name) / "desk.db",
                dist_dir=self.dist_dir,
                data_dir=Path(self.tmp.name) / "desk_data",
                start_monitor=False,
            )

            import socket
            import uvicorn

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            cfg = uvicorn.Config(desk_app, host="127.0.0.1", port=port, log_level="error")
            srv = uvicorn.Server(cfg)
            th = threading.Thread(target=srv.run, daemon=True)
            th.start()
            for _ in range(30):
                if srv.started:
                    break
                time.sleep(0.1)

            try:
                tok = orch_cfg.ensure_node_token(desk_cfg_dir)
                events: list[dict] = []

                async def _emit(ev: dict) -> None:
                    events.append(ev)

                res = asyncio.run(
                    inventory_sync.run_desktop_catchup(
                        conn,
                        f"http://127.0.0.1:{port}",
                        {"X-Node-Token": tok},
                        _emit,
                        self.gh_dir,
                    )
                )
                self.assertEqual(res["processed"], 2)
                self.assertEqual(res["synced"], 1)
                self.assertEqual(res["conflicts"], 1)
                q = inventory_sync.list_intermission_queue(conn)
                by_id = {x["id"]: x for x in q}
                self.assertEqual(by_id[eid1]["status"], "synced")
                self.assertEqual(by_id[eid2]["status"], "conflict_needs_agent")
            finally:
                srv.should_exit = True
                th.join(timeout=3)
        finally:
            conn.close()

    def test_builtin_orchestrator_tools_passive_capabilities(self) -> None:
        conn = orch_cfg.init_db(self.db_path)
        try:
            settings = orch_cfg.load_settings(self.cfg_dir / "settings.json")
            prompt = orch_tools.build_orchestrator_system_prompt("Base.", conn, settings, False, {})
            self.assertIn("Live Orchestrator Environment", prompt)
            self.assertIn("OFFLINE", prompt)
            self.assertIn("dolphin-llama3:8B", prompt)

            async def _noop_cli(**_kwargs):
                return {}

            async def _noop_sync(_r, _a, _n, _ag):
                return {}

            async def _noop_specialist(tk, mid, _n, p):
                return {"taskType": tk, "model": mid or "dolphin-llama3:8B", "response": f"Story for {p}"}

            caps = asyncio.run(
                orch_tools.execute_builtin_tool(
                    "orch_list_capabilities",
                    {"refresh": False},
                    conn=conn,
                    settings=settings,
                    node_id="laptop",
                    github_dir=self.gh_dir,
                    vault_dir=self.vault_dir,
                    desktop_online=False,
                    tools_reg={},
                    dispatch_cli_agent=_noop_cli,
                    sync_repo_fn=_noop_sync,
                    run_specialist_llm_fn=_noop_specialist,
                )
            )
            self.assertIn("computers", caps)
            self.assertIn("agySubModels", caps)
            self.assertIn("configuredRouting", caps)
            self.assertEqual(caps["configuredRouting"]["story"]["model"], "dolphin-llama3:8B")

            cfg_read = asyncio.run(
                orch_tools.execute_builtin_tool(
                    "orch_get_settings",
                    {},
                    conn=conn,
                    settings=settings,
                    node_id="laptop",
                    github_dir=self.gh_dir,
                    vault_dir=self.vault_dir,
                    desktop_online=False,
                    tools_reg={},
                    dispatch_cli_agent=_noop_cli,
                    sync_repo_fn=_noop_sync,
                    run_specialist_llm_fn=_noop_specialist,
                )
            )
            self.assertEqual(cfg_read["taskDefaults"]["story"]["modelId"], "dolphin-llama3:8B")

            # Verify orch_run_command executes cleanly
            async def _fake_run_cmd(tnode: str, cmd: str, cwd: str):
                return {"executedNode": tnode, "exitCode": 0, "output": f"ran:{cmd} in {cwd}"}

            cmd_res = asyncio.run(
                orch_tools.execute_builtin_tool(
                    "orch_run_command",
                    {"command": "git status -s", "targetNode": "laptop"},
                    conn=conn,
                    settings=settings,
                    node_id="laptop",
                    github_dir=self.gh_dir,
                    vault_dir=self.vault_dir,
                    desktop_online=False,
                    tools_reg={},
                    dispatch_cli_agent=_noop_cli,
                    sync_repo_fn=_noop_sync,
                    run_specialist_llm_fn=_noop_specialist,
                    run_command_fn=_fake_run_cmd,
                )
            )
            self.assertEqual(cmd_res["exitCode"], 0)
            self.assertIn("git status -s", cmd_res["output"])

            # Verify orch_get_repo_status merges local scan + remote GitHub list cleanly
            orig_list_remote = orch_tools.list_github_remote_repos
            orch_tools.list_github_remote_repos = lambda limit=250: {  # type: ignore[assignment]
                "available": True,
                "repos": [
                    {"name": "demo-app", "private": True, "defaultBranch": "main", "pushedAt": "", "url": ""},
                    {"name": "cloud-only-repo", "private": False, "defaultBranch": "main", "pushedAt": "", "url": ""},
                ],
                "error": None,
            }
            try:
                repo_stat = asyncio.run(
                    orch_tools.execute_builtin_tool(
                        "orch_get_repo_status",
                        {"refresh": True, "includeRemote": True},
                        conn=conn,
                        settings=settings,
                        node_id="laptop",
                        github_dir=self.gh_dir,
                        vault_dir=self.vault_dir,
                        desktop_online=False,
                        tools_reg={},
                        dispatch_cli_agent=_noop_cli,
                        sync_repo_fn=_noop_sync,
                        run_specialist_llm_fn=_noop_specialist,
                        run_command_fn=_fake_run_cmd,
                    )
                )
            finally:
                orch_tools.list_github_remote_repos = orig_list_remote  # type: ignore[assignment]
            self.assertEqual(repo_stat["counts"]["laptopTotal"], 1)
            self.assertEqual(repo_stat["counts"]["githubRemoteTotal"], 2)
            self.assertEqual(repo_stat["remoteOnlyRepos"], ["cloud-only-repo"])
        finally:
            conn.close()


    def test_repo_case_insensitive_compare_and_clean_model_profiles(self) -> None:
        cmp_rows = inventory_sync.compare_repo_inventories(
            [
                {
                    "nodeId": "laptop",
                    "repoName": "vibebuilder-ide",
                    "branch": "main",
                    "headSha": "abc123",
                    "dirtyCount": 0,
                    "ahead": 0,
                    "behind": 0,
                },
                {
                    "nodeId": "desktop",
                    "repoName": "VibeBuilder-IDE",
                    "branch": "main",
                    "headSha": "abc123",
                    "dirtyCount": 0,
                    "ahead": 0,
                    "behind": 0,
                },
            ]
        )
        self.assertEqual(len(cmp_rows), 1)
        self.assertEqual(cmp_rows[0]["repoName"], "VibeBuilder-IDE")
        self.assertEqual(cmp_rows[0]["status"], "in_sync")

        p_coder = orch_cfg.infer_model_profile("qwen2.5-coder:14b")
        self.assertEqual(p_coder["displayName"], "Qwen2.5 Coder 14B")
        self.assertIn("Recommended", p_coder["strengths"])
        p_hf = orch_cfg.infer_model_profile("hf.co/dphn/Dolphin-X1-8B-GGUF:Q5_K_M")
        self.assertEqual(p_hf["displayName"], "Dolphin-X1 8B (Q5_K_M)")
        p_embed = orch_cfg.infer_model_profile("mxbai-embed-large:latest", kind="embed")
        self.assertEqual(p_embed["strengths"], ["Embedding"])

    def test_large_ndjson_line_exceeding_64kb_chunk_limit(self) -> None:
        huge = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 180_000}]}}).encode() + b"\n"

        async def _collect() -> list[bytes]:
            reader = asyncio.StreamReader(limit=65536)
            reader.feed_data(huge)
            reader.feed_eof()
            out = []
            async for ln in cli_agents._iter_ndjson_lines(reader):
                out.append(ln)
            return out

        lines = asyncio.run(_collect())
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(json.loads(lines[0])["message"]["content"][0]["text"]), 180_000)

    def test_repo_tracker_holds_sha256_and_api_endpoints(self) -> None:
        from server import repo_ops, repo_tracker

        tconn = repo_tracker.init_tracker_db(self.data_dir / "repo_status.db")
        try:
            items = repo_tracker.scan_all_repos_tracker(
                tconn, self.gh_dir, "laptop", data_dir=self.data_dir, vault_dir=self.vault_dir, check_remote=False
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["repoName"], "demo-app")
            rows = repo_tracker.read_three_way_status(tconn)
            self.assertEqual(len(rows), 1)

            repo_tracker.set_repo_hold(tconn, "demo-app", "active", set_by="user", note="in progress", data_dir=self.data_dir, vault_dir=self.vault_dir)
            held, note = repo_tracker.is_repo_held(tconn, "demo-app")
            self.assertTrue(held)
            self.assertIn("in progress", note)
            self.assertIn("demo-app", (self.data_dir / "repo_holds.txt").read_text(encoding="utf-8"))
            self.assertTrue((self.vault_dir / "holds.json").is_file())
        finally:
            tconn.close()

        # Verify bundle SHA-256 mismatch detection in repo_ops.apply_handoff
        demo = self.gh_dir / "demo-app"
        head = repo_ops.git(demo, "rev-parse", "HEAD")
        bundle = repo_ops.make_bundle(demo, ["refs/heads/main" if repo_ops.git_ok(demo, "rev-parse", "--verify", "refs/heads/main") else "refs/heads/master"])
        with self.assertRaises(repo_ops.RepoOpError):
            repo_ops.apply_handoff(demo, "desktop", "laptop", bundle, "master", head, "", expected_bundle_sha256="0" * 64)

        # Verify API routes enforce active holds on pull and expose 3-way tracker
        app = orch_app.create_app(
            config_dir=self.cfg_dir,
            db_path=self.db_path,
            dist_dir=self.dist_dir,
            data_dir=self.data_dir,
            start_monitor=False,
        )
        with TestClient(app, base_url="http://127.0.0.1:8790") as client:
            hdrs = {"X-Orch-Client": "1", "X-Node-Token": app.state.ctx.token()}
            res = client.post("/api/repos/holds", json={"repoName": "demo-app", "state": "active", "note": "testing hold"}, headers=hdrs)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["counts"]["active"], 1)

            pull_res = client.post("/api/sync/git-pull", json={"repoName": "demo-app"}, headers=hdrs)
            self.assertEqual(pull_res.status_code, 200)
            self.assertTrue(pull_res.json().get("held"))

            trk = client.get("/api/repos/tracker?refresh=true", headers=hdrs).json()
            self.assertEqual(trk["counts"]["active"], 1)
            self.assertEqual(trk["repos"][0]["repoName"], "demo-app")

            st = client.get("/api/status", headers=hdrs).json()
            self.assertEqual(st["repoHoldsCount"], 1)


if __name__ == "__main__":
    unittest.main()
