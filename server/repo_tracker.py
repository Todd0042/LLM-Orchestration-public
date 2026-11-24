"""Strictly read-only three-way Git repository tracker (Laptop <-> Desktop <-> GitHub Remote) & active holds.

Stored in its own `repo_status.db` file so frequent background scans stay isolated from `orchestration.db`.
Safety guarantees:
  * Every git invocation passes `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0` so background scans
    never block or contend for `index.lock` with active coding agents or user commits.
  * Diffs are stored as `git diff --stat` summaries only (never full patches).
  * Remote GitHub branch heads come from read-only `git ls-remote --heads` (never `git fetch` or mutating ops).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any


SNAPSHOT_RETENTION_SEC = 30 * 86400
DEFAULT_SCAN_TIMEOUT_SEC = 10.0


def repo_status_db_path(data_dir: Path) -> Path:
    return data_dir / "repo_status.db"


def init_tracker_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            node_id TEXT NOT NULL,
            scanned_at REAL NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            remote_head_sha TEXT NOT NULL DEFAULT '',
            ahead INTEGER NOT NULL DEFAULT 0,
            behind INTEGER NOT NULL DEFAULT 0,
            dirty_count INTEGER NOT NULL DEFAULT 0,
            untracked_count INTEGER NOT NULL DEFAULT 0,
            stash_count INTEGER NOT NULL DEFAULT 0,
            worktree_count INTEGER NOT NULL DEFAULT 1,
            detached INTEGER NOT NULL DEFAULT 0,
            last_commit_at REAL NOT NULL DEFAULT 0,
            last_change_at REAL NOT NULL DEFAULT 0,
            active_process TEXT NOT NULL DEFAULT '',
            diff_stat TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_lookup ON snapshots(repo_key, node_id, scanned_at DESC);

        CREATE TABLE IF NOT EXISTS branches (
            repo_key TEXT NOT NULL,
            location TEXT NOT NULL,
            branch TEXT NOT NULL,
            sha TEXT NOT NULL,
            scanned_at REAL NOT NULL,
            PRIMARY KEY (repo_key, location, branch)
        );

        CREATE TABLE IF NOT EXISTS meta (
            repo_key TEXT PRIMARY KEY,
            repo_name TEXT NOT NULL,
            remote_url TEXT NOT NULL DEFAULT '',
            has_submodules INTEGER NOT NULL DEFAULT 0,
            has_lfs INTEGER NOT NULL DEFAULT 0,
            size_kb INTEGER NOT NULL DEFAULT 0,
            newest_mtime REAL NOT NULL DEFAULT 0,
            active_process TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS holds (
            repo_key TEXT PRIMARY KEY,
            repo_name TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'needs_answer',
            set_by TEXT NOT NULL DEFAULT '',
            set_at REAL NOT NULL DEFAULT 0,
            last_change_at REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


def _ro_git(repo: Path, *args: str, timeout: float = DEFAULT_SCAN_TIMEOUT_SEC) -> tuple[int, str]:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def _running_agent_processes() -> list[str]:
    try:
        proc = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=3, check=False)
        lines = proc.stdout.splitlines() if proc.returncode == 0 else []
    except Exception:
        lines = []
    return [ln for ln in lines if ("claude" in ln or "agy" in ln)]


def _newest_worktree_mtime(repo: Path) -> float:
    newest = 0.0
    try:
        newest = max(newest, repo.stat().st_mtime)
        git_head = repo / ".git" / "HEAD"
        if git_head.exists():
            newest = max(newest, git_head.stat().st_mtime)
        idx = repo / ".git" / "index"
        if idx.exists():
            newest = max(newest, idx.stat().st_mtime)
    except OSError:
        pass
    # Quick shallow stat of modified/untracked files reported by git status --porcelain
    rc, status_out = _ro_git(repo, "status", "--porcelain")
    if rc == 0 and status_out:
        for line in status_out.splitlines()[:100]:
            rel = line[3:].strip().split(" -> ")[-1].strip('"')
            if not rel:
                continue
            try:
                p = repo / rel
                if p.exists():
                    newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    return newest


def scan_single_repo_tracker(repo_dir: Path, node_id: str, proc_lines: list[str] | None = None, check_remote: bool = True) -> dict[str, Any]:
    now = time.time()
    name = repo_dir.name
    rc_b, branch = _ro_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    detached = 1 if (rc_b == 0 and branch == "HEAD") else 0
    if not branch:
        branch = "HEAD"

    _, head_sha = _ro_git(repo_dir, "rev-parse", "HEAD")
    _, ab_out = _ro_git(repo_dir, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    ahead, behind = 0, 0
    if ab_out:
        parts = ab_out.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ahead, behind = int(parts[0]), int(parts[1])

    _, porcelain = _ro_git(repo_dir, "status", "--porcelain")
    dirty_lines = porcelain.splitlines() if porcelain else []
    untracked_count = sum(1 for ln in dirty_lines if ln.startswith("??"))
    dirty_count = len(dirty_lines) - untracked_count

    _, diff_stat = _ro_git(repo_dir, "diff", "--stat", "--stat-width=100", "HEAD")
    diff_stat = "\n".join(diff_stat.splitlines()[:40])[:3500]

    _, stash_out = _ro_git(repo_dir, "stash", "list")
    stash_count = len(stash_out.splitlines()) if stash_out else 0

    _, wt_out = _ro_git(repo_dir, "worktree", "list", "--porcelain")
    worktree_count = sum(1 for ln in wt_out.splitlines() if ln.startswith("worktree ")) or 1

    _, c_ts = _ro_git(repo_dir, "log", "-1", "--format=%ct")
    last_commit_at = float(c_ts) if c_ts.isdigit() else 0.0
    newest_mtime = _newest_worktree_mtime(repo_dir)
    last_change_at = max(last_commit_at, newest_mtime)

    # All local branch heads
    _, refs_out = _ro_git(repo_dir, "for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads")
    local_branches: dict[str, str] = {}
    for ln in refs_out.splitlines():
        if "\t" in ln:
            bname, bsha = ln.split("\t", 1)
            local_branches[bname.strip()] = bsha.strip()

    _, remote_url = _ro_git(repo_dir, "remote", "get-url", "origin")
    remote_branches: dict[str, str] = {}
    remote_head_sha = ""
    if check_remote and remote_url:
        rc_ls, ls_out = _ro_git(repo_dir, "ls-remote", "--heads", "origin", timeout=8.0)
        if rc_ls == 0 and ls_out:
            for ln in ls_out.splitlines():
                parts = ln.split("\t")
                if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                    rb = parts[1][len("refs/heads/") :]
                    remote_branches[rb] = parts[0].strip()
            remote_head_sha = remote_branches.get(branch, "")

    procs = proc_lines if proc_lines is not None else _running_agent_processes()
    active_proc = ""
    repo_str = str(repo_dir)
    for ln in procs:
        if repo_str in ln or f"/{name}" in ln:
            active_proc = "claude" if "claude" in ln else "agy"
            break

    has_submodules = 1 if (repo_dir / ".gitmodules").is_file() else 0
    has_lfs = 1 if ((repo_dir / ".git" / "lfs").is_dir()) else 0

    return {
        "repoKey": name.lower(),
        "repoName": name,
        "nodeId": node_id,
        "scannedAt": now,
        "branch": branch,
        "headSha": head_sha,
        "remoteHeadSha": remote_head_sha,
        "ahead": ahead,
        "behind": behind,
        "dirtyCount": dirty_count,
        "untrackedCount": untracked_count,
        "stashCount": stash_count,
        "worktreeCount": worktree_count,
        "detached": detached,
        "lastCommitAt": last_commit_at,
        "lastChangeAt": last_change_at,
        "activeProcess": active_proc,
        "diffStat": diff_stat,
        "remoteUrl": remote_url,
        "hasSubmodules": has_submodules,
        "hasLfs": has_lfs,
        "newestMtime": newest_mtime,
        "localBranches": local_branches,
        "remoteBranches": remote_branches,
    }


def _write_holds_files(conn: sqlite3.Connection, data_dir: Path, vault_dir: Path | None) -> None:
    rows = conn.execute("SELECT repo_key, repo_name, state, set_by, set_at, last_change_at, note FROM holds ORDER BY last_change_at DESC").fetchall()
    active_names = [r["repo_name"] for r in rows if r["state"] == "active"]
    holds_txt = data_dir / "repo_holds.txt"
    holds_txt.parent.mkdir(parents=True, exist_ok=True)
    holds_txt.write_text("\n".join(active_names) + ("\n" if active_names else ""), encoding="utf-8")
    if vault_dir:
        try:
            vault_dir.mkdir(parents=True, exist_ok=True)
            payload = [
                {
                    "repoKey": r["repo_key"],
                    "repoName": r["repo_name"],
                    "state": r["state"],
                    "setBy": r["set_by"],
                    "setAt": r["set_at"],
                    "lastChangeAt": r["last_change_at"],
                    "note": r["note"],
                }
                for r in rows
            ]
            (vault_dir / "holds.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass


def scan_all_repos_tracker(
    conn: sqlite3.Connection,
    github_dir: Path,
    node_id: str,
    *,
    data_dir: Path | None = None,
    vault_dir: Path | None = None,
    check_remote: bool = True,
) -> list[dict[str, Any]]:
    now = time.time()
    proc_lines = _running_agent_processes()
    items: list[dict[str, Any]] = []
    if github_dir.is_dir():
        dirs = sorted(
            [p for p in github_dir.iterdir() if p.is_dir() and (p / ".git").exists()],
            key=lambda p: p.name.lower(),
        )
        for repo_dir in dirs:
            items.append(scan_single_repo_tracker(repo_dir, node_id, proc_lines=proc_lines, check_remote=check_remote))

    cutoff = now - SNAPSHOT_RETENTION_SEC
    conn.execute("DELETE FROM snapshots WHERE scanned_at < ?", (cutoff,))

    for r in items:
        key = r["repoKey"]
        conn.execute(
            """INSERT INTO snapshots
            (repo_key, repo_name, node_id, scanned_at, branch, head_sha, remote_head_sha, ahead, behind,
             dirty_count, untracked_count, stash_count, worktree_count, detached, last_commit_at, last_change_at, active_process, diff_stat)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                r["repoName"],
                node_id,
                r["scannedAt"],
                r["branch"],
                r["headSha"],
                r["remoteHeadSha"],
                r["ahead"],
                r["behind"],
                r["dirtyCount"],
                r["untrackedCount"],
                r["stashCount"],
                r["worktreeCount"],
                r["detached"],
                r["lastCommitAt"],
                r["lastChangeAt"],
                r["activeProcess"],
                r["diffStat"],
            ),
        )
        conn.execute("DELETE FROM branches WHERE repo_key=? AND location=?", (key, node_id))
        for bname, bsha in r["localBranches"].items():
            conn.execute(
                "INSERT OR REPLACE INTO branches (repo_key, location, branch, sha, scanned_at) VALUES (?,?,?,?,?)",
                (key, node_id, bname, bsha, r["scannedAt"]),
            )
        if r["remoteBranches"]:
            conn.execute("DELETE FROM branches WHERE repo_key=? AND location='github'", (key,))
            for bname, bsha in r["remoteBranches"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO branches (repo_key, location, branch, sha, scanned_at) VALUES (?,?,?,?,?)",
                    (key, "github", bname, bsha, r["scannedAt"]),
                )
        conn.execute(
            """INSERT INTO meta (repo_key, repo_name, remote_url, has_submodules, has_lfs, size_kb, newest_mtime, active_process, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repo_key) DO UPDATE SET
                repo_name=excluded.repo_name,
                remote_url=CASE WHEN excluded.remote_url != '' THEN excluded.remote_url ELSE meta.remote_url END,
                has_submodules=excluded.has_submodules,
                has_lfs=excluded.has_lfs,
                newest_mtime=MAX(meta.newest_mtime, excluded.newest_mtime),
                active_process=excluded.active_process,
                updated_at=excluded.updated_at""",
            (key, r["repoName"], r["remoteUrl"], r["hasSubmodules"], r["hasLfs"], 0, r["newestMtime"], r["activeProcess"], now),
        )
        # Flag newly dirty / active-process repos as needs_answer if no decision exists yet
        needs_flag = (r["dirtyCount"] + r["untrackedCount"] > 0) or bool(r["activeProcess"])
        existing_hold = conn.execute("SELECT state FROM holds WHERE repo_key=?", (key,)).fetchone()
        if not existing_hold and needs_flag:
            note = f"active {r['activeProcess']}" if r["activeProcess"] else f"{r['dirtyCount'] + r['untrackedCount']} changed on {node_id}"
            conn.execute(
                "INSERT INTO holds (repo_key, repo_name, state, set_by, set_at, last_change_at, note) VALUES (?,?,?,?,?,?,?)",
                (key, r["repoName"], "needs_answer", node_id, now, r["lastChangeAt"], note),
            )
        elif existing_hold:
            conn.execute("UPDATE holds SET last_change_at=MAX(last_change_at, ?) WHERE repo_key=?", (r["lastChangeAt"], key))

    conn.commit()
    if data_dir:
        _write_holds_files(conn, data_dir, vault_dir)
    return items


def ingest_peer_tracker_payload(conn: sqlite3.Connection, peer_node_id: str, payload: dict[str, Any], data_dir: Path | None = None, vault_dir: Path | None = None) -> None:
    """Merge snapshots, branches, meta, and holds synced from another node over :8790."""
    for s in payload.get("snapshots") or []:
        key = str(s.get("repoKey") or str(s.get("repoName") or "").lower())
        if not key:
            continue
        conn.execute(
            """INSERT INTO snapshots
            (repo_key, repo_name, node_id, scanned_at, branch, head_sha, remote_head_sha, ahead, behind,
             dirty_count, untracked_count, stash_count, worktree_count, detached, last_commit_at, last_change_at, active_process, diff_stat)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                str(s.get("repoName") or key),
                peer_node_id,
                float(s.get("scannedAt") or time.time()),
                str(s.get("branch") or ""),
                str(s.get("headSha") or ""),
                str(s.get("remoteHeadSha") or ""),
                int(s.get("ahead") or 0),
                int(s.get("behind") or 0),
                int(s.get("dirtyCount") or 0),
                int(s.get("untrackedCount") or 0),
                int(s.get("stashCount") or 0),
                int(s.get("worktreeCount") or 1),
                int(s.get("detached") or 0),
                float(s.get("lastCommitAt") or 0),
                float(s.get("lastChangeAt") or 0),
                str(s.get("activeProcess") or ""),
                str(s.get("diffStat") or ""),
            ),
        )
    for b in payload.get("branches") or []:
        conn.execute(
            "INSERT OR REPLACE INTO branches (repo_key, location, branch, sha, scanned_at) VALUES (?,?,?,?,?)",
            (str(b["repoKey"]), str(b["location"]), str(b["branch"]), str(b["sha"]), float(b.get("scannedAt") or time.time())),
        )
    for m in payload.get("meta") or []:
        conn.execute(
            """INSERT INTO meta (repo_key, repo_name, remote_url, has_submodules, has_lfs, size_kb, newest_mtime, active_process, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repo_key) DO UPDATE SET
                newest_mtime=MAX(meta.newest_mtime, excluded.newest_mtime),
                active_process=COALESCE(NULLIF(excluded.active_process,''), meta.active_process),
                updated_at=MAX(meta.updated_at, excluded.updated_at)""",
            (
                str(m["repoKey"]),
                str(m["repoName"]),
                str(m.get("remoteUrl") or ""),
                int(m.get("hasSubmodules") or 0),
                int(m.get("hasLfs") or 0),
                int(m.get("sizeKb") or 0),
                float(m.get("newestMtime") or 0),
                str(m.get("activeProcess") or ""),
                float(m.get("updatedAt") or time.time()),
            ),
        )
    for h in payload.get("holds") or []:
        cur = conn.execute("SELECT set_at FROM holds WHERE repo_key=?", (str(h["repoKey"]),)).fetchone()
        if not cur or float(h.get("setAt") or 0) > float(cur["set_at"] or 0):
            conn.execute(
                """INSERT INTO holds (repo_key, repo_name, state, set_by, set_at, last_change_at, note)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(repo_key) DO UPDATE SET
                    state=excluded.state, set_by=excluded.set_by, set_at=excluded.set_at,
                    last_change_at=MAX(holds.last_change_at, excluded.last_change_at), note=excluded.note""",
                (str(h["repoKey"]), str(h["repoName"]), str(h["state"]), str(h.get("setBy") or ""),
                 float(h.get("setAt") or time.time()), float(h.get("lastChangeAt") or 0), str(h.get("note") or "")),
            )
    conn.commit()
    if data_dir:
        _write_holds_files(conn, data_dir, vault_dir)


def export_local_tracker_payload(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    snaps = conn.execute(
        """SELECT s.* FROM snapshots s
           JOIN (SELECT repo_key, MAX(id) AS mid FROM snapshots WHERE node_id=? GROUP BY repo_key) latest
           ON s.id = latest.mid""",
        (node_id,),
    ).fetchall()
    branches = conn.execute("SELECT * FROM branches WHERE location IN (?, 'github')", (node_id,)).fetchall()
    metas = conn.execute("SELECT * FROM meta").fetchall()
    holds = conn.execute("SELECT * FROM holds").fetchall()
    return {
        "nodeId": node_id,
        "snapshots": [
            {
                "repoKey": r["repo_key"], "repoName": r["repo_name"], "nodeId": r["node_id"],
                "scannedAt": r["scanned_at"], "branch": r["branch"], "headSha": r["head_sha"],
                "remoteHeadSha": r["remote_head_sha"], "ahead": r["ahead"], "behind": r["behind"],
                "dirtyCount": r["dirty_count"], "untrackedCount": r["untracked_count"],
                "stashCount": r["stash_count"], "worktreeCount": r["worktree_count"],
                "detached": r["detached"], "lastCommitAt": r["last_commit_at"],
                "lastChangeAt": r["last_change_at"], "activeProcess": r["active_process"], "diffStat": r["diff_stat"],
            }
            for r in snaps
        ],
        "branches": [{"repoKey": b["repo_key"], "location": b["location"], "branch": b["branch"], "sha": b["sha"], "scannedAt": b["scanned_at"]} for b in branches],
        "meta": [
            {
                "repoKey": m["repo_key"], "repoName": m["repo_name"], "remoteUrl": m["remote_url"],
                "hasSubmodules": m["has_submodules"], "hasLfs": m["has_lfs"], "sizeKb": m["size_kb"],
                "newestMtime": m["newest_mtime"], "activeProcess": m["active_process"], "updatedAt": m["updated_at"],
            }
            for m in metas
        ],
        "holds": [
            {
                "repoKey": h["repo_key"], "repoName": h["repo_name"], "state": h["state"],
                "setBy": h["set_by"], "setAt": h["set_at"], "lastChangeAt": h["last_change_at"], "note": h["note"],
            }
            for h in holds
        ],
    }


def set_repo_hold(
    conn: sqlite3.Connection,
    repo_name: str,
    state: str,
    *,
    set_by: str,
    note: str = "",
    data_dir: Path | None = None,
    vault_dir: Path | None = None,
) -> dict[str, Any]:
    if state not in ("active", "clear", "needs_answer"):
        raise ValueError(f"Unsupported hold state: {state}")
    key = Path(repo_name).name.lower()
    now = time.time()
    snap = conn.execute("SELECT MAX(last_change_at) AS lc, MAX(repo_name) AS rn FROM snapshots WHERE repo_key=?", (key,)).fetchone()
    last_change = float(snap["lc"] or 0) if snap else 0.0
    clean_name = str((snap["rn"] if snap and snap["rn"] else Path(repo_name).name))
    conn.execute(
        """INSERT INTO holds (repo_key, repo_name, state, set_by, set_at, last_change_at, note)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(repo_key) DO UPDATE SET
            repo_name=excluded.repo_name, state=excluded.state, set_by=excluded.set_by,
            set_at=excluded.set_at, last_change_at=MAX(holds.last_change_at, excluded.last_change_at), note=excluded.note""",
        (key, clean_name, state, set_by, now, last_change, note[:300]),
    )
    conn.commit()
    if data_dir:
        _write_holds_files(conn, data_dir, vault_dir)
    return {"repoKey": key, "repoName": clean_name, "state": state, "setBy": set_by, "setAt": now, "lastChangeAt": last_change, "note": note}


def get_repo_hold(conn: sqlite3.Connection, repo_name: str) -> dict[str, Any] | None:
    key = Path(repo_name).name.lower()
    r = conn.execute("SELECT * FROM holds WHERE repo_key=?", (key,)).fetchone()
    if not r:
        return None
    return {
        "repoKey": r["repo_key"],
        "repoName": r["repo_name"],
        "state": r["state"],
        "setBy": r["set_by"],
        "setAt": r["set_at"],
        "lastChangeAt": r["last_change_at"],
        "note": r["note"],
    }


def is_repo_held(conn: sqlite3.Connection, repo_name: str) -> tuple[bool, str]:
    h = get_repo_hold(conn, repo_name)
    if h and h["state"] == "active":
        return True, h.get("note") or "locked under active development"
    return False, ""


def read_three_way_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Build merged Laptop <-> Desktop <-> GitHub Remote status sorted by lastChangeAt descending."""
    snaps = conn.execute(
        """SELECT s.* FROM snapshots s
           JOIN (SELECT repo_key, node_id, MAX(id) AS mid FROM snapshots GROUP BY repo_key, node_id) latest
           ON s.id = latest.mid"""
    ).fetchall()
    branches_rows = conn.execute("SELECT * FROM branches").fetchall()
    metas = {m["repo_key"]: dict(m) for m in conn.execute("SELECT * FROM meta").fetchall()}
    holds = {h["repo_key"]: dict(h) for h in conn.execute("SELECT * FROM holds").fetchall()}

    by_repo: dict[str, dict[str, Any]] = {}
    for r in snaps:
        key = r["repo_key"]
        entry = by_repo.setdefault(
            key,
            {"repoKey": key, "repoName": r["repo_name"], "nodes": {}, "branches": {}, "lastChangeAt": 0.0, "activeProcess": ""},
        )
        if entry["repoName"].islower() and not r["repo_name"].islower():
            entry["repoName"] = r["repo_name"]
        entry["nodes"][r["node_id"]] = {
            "branch": r["branch"],
            "headSha": r["head_sha"][:8],
            "remoteHeadSha": r["remote_head_sha"][:8],
            "ahead": r["ahead"],
            "behind": r["behind"],
            "dirtyCount": r["dirty_count"],
            "untrackedCount": r["untracked_count"],
            "stashCount": r["stash_count"],
            "worktreeCount": r["worktree_count"],
            "detached": bool(r["detached"]),
            "lastCommitAt": r["last_commit_at"],
            "lastChangeAt": r["last_change_at"],
            "activeProcess": r["active_process"],
            "diffStat": r["diff_stat"],
            "scannedAt": r["scanned_at"],
        }
        entry["lastChangeAt"] = max(entry["lastChangeAt"], float(r["last_change_at"] or 0))
        if r["active_process"]:
            entry["activeProcess"] = r["active_process"]

    for b in branches_rows:
        entry = by_repo.get(b["repo_key"])
        if entry:
            entry["branches"].setdefault(b["location"], {})[b["branch"]] = b["sha"][:8]

    out: list[dict[str, Any]] = []
    for key, entry in by_repo.items():
        nodes = entry["nodes"]
        lap = nodes.get("laptop")
        desk = nodes.get("desktop")
        gh_branches = entry["branches"].get("github") or {}
        primary_branch = (lap or desk or {}).get("branch") or "main"
        gh_sha = gh_branches.get(primary_branch, "")

        dirty = any((v["dirtyCount"] + v["untrackedCount"]) > 0 for v in nodes.values())
        heads = {v["headSha"] for v in nodes.values() if v["headSha"]}
        if gh_sha:
            all_match_remote = all(v["headSha"] == gh_sha for v in nodes.values() if v["headSha"])
        else:
            all_match_remote = len(heads) <= 1

        branches_differ = len({v["branch"] for v in nodes.values()}) > 1
        if dirty:
            status = "dirty"
        elif branches_differ:
            status = "branch_mismatch"
        elif len(nodes) == 2 and len(heads) == 1 and all_match_remote:
            status = "in_sync"
        elif len(nodes) == 1:
            status = "single_node"
        else:
            status = "diverged"

        hold = holds.get(key)
        meta = metas.get(key) or {}
        entry["lastChangeAt"] = max(entry["lastChangeAt"], float(meta.get("newest_mtime") or 0))
        out.append(
            {
                "repoKey": key,
                "repoName": entry["repoName"],
                "threeWayStatus": status,
                "laptop": lap,
                "desktop": desk,
                "github": {"branch": primary_branch, "headSha": gh_sha, "branchCount": len(gh_branches)} if gh_branches else None,
                "branches": entry["branches"],
                "remoteUrl": meta.get("remote_url", ""),
                "activeProcess": entry["activeProcess"] or meta.get("active_process", ""),
                "lastChangeAt": entry["lastChangeAt"],
                "hold": (
                    {
                        "state": hold["state"],
                        "setBy": hold["set_by"],
                        "setAt": hold["set_at"],
                        "note": hold["note"],
                    }
                    if hold
                    else None
                ),
            }
        )
    out.sort(key=lambda r: (-float(r["lastChangeAt"] or 0), r["repoName"].lower()))
    return out


def hold_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT state, COUNT(*) AS c FROM holds GROUP BY state").fetchall()
    counts = {r["state"]: int(r["c"]) for r in rows}
    return {"active": counts.get("active", 0), "needsAnswer": counts.get("needs_answer", 0)}


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    import sys
    from .config import DATA_DIR, load_settings

    s = load_settings()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        db_conn = init_tracker_db(repo_status_db_path(DATA_DIR))
        try:
            res = scan_all_repos_tracker(
                db_conn,
                Path(str(s["paths"].get("githubDir") or (Path.home() / "GitHub"))).expanduser(),
                str(s["node"]["id"]),
                data_dir=DATA_DIR,
                vault_dir=Path(str(s["paths"].get("vaultDir") or (Path.home() / "LLM-Vault"))).expanduser(),
                check_remote=("--no-remote" not in sys.argv),
            )
            print(json.dumps({"scanned": len(res), "holds": hold_counts(db_conn)}))
        finally:
            db_conn.close()
