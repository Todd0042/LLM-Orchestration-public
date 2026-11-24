"""Local, non-destructive git operations used by the repo handoff engine (run on each node).

Safety rules enforced here:
  * never `reset --hard`, never delete branches, never force-push anything except our own wip/ refs
  * uncommitted work is captured into a commit object via a temporary index: the working tree
    and the real index are left exactly as they were
  * before replacing a dirty working tree, it must be byte-identical to the snapshot we handed
    off from it last time; even then it's stashed first (recoverable with `git stash list`)
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from .inventory_sync import scan_single_repo


class RepoOpError(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: float = 60.0, env: dict[str, str] | None = None, input_bytes: bytes | None = None, check: bool = True) -> str:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", **(env or {})}
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout, env=full_env, input=input_bytes)
    if check and p.returncode != 0:
        raise RepoOpError(f"git {' '.join(args[:3])} failed: {p.stderr.decode(errors='replace').strip()[:400]}")
    return p.stdout.decode(errors="replace").strip()


def git_ok(repo: Path, *args: str, timeout: float = 30.0) -> bool:
    try:
        git(repo, *args, timeout=timeout)
        return True
    except (RepoOpError, subprocess.TimeoutExpired):
        return False


def find_repo(github_dir: Path, name: str) -> Path | None:
    clean = Path(name).name
    direct = github_dir / clean
    if (direct / ".git").exists():
        return direct
    if github_dir.is_dir():
        for p in github_dir.iterdir():
            if p.name.lower() == clean.lower() and (p / ".git").exists():
                return p
    return None


def _ref_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_./" else "_" for ch in s).strip("/.") or "x"


def worktree_tree(repo: Path) -> str:
    """Tree object of the full working copy (tracked + untracked, honoring .gitignore)."""
    with tempfile.TemporaryDirectory() as td:
        idx = str(Path(td) / "index")
        env = {"GIT_INDEX_FILE": idx}
        if git_ok(repo, "rev-parse", "--verify", "HEAD"):
            git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "-A", ".", env=env, timeout=120)
        return git(repo, "write-tree", env=env)


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_fingerprint(repo: Path) -> str:
    """Compute a deterministic SHA-256 checksum over the repo working tree + HEAD commit."""
    head = git(repo, "rev-parse", "HEAD", check=False)
    tree = worktree_tree(repo) if head else ""
    payload = f"{head}\n{tree}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def snapshot(repo: Path, node_id: str) -> dict[str, Any]:
    info = scan_single_repo(repo, node_id)
    head = git(repo, "rev-parse", "HEAD", check=False)
    info["headFull"] = head
    dirty = info["dirtyCount"] + info["untrackedCount"] > 0
    info["dirty"] = dirty
    info["treeSha"] = worktree_tree(repo) if head else ""
    info["treeSha256"] = hashlib.sha256(f"{head}\n{info['treeSha']}".encode("utf-8")).hexdigest() if head else ""
    applied = git(repo, "rev-parse", "--verify", "-q", f"refs/orch/applied/{_ref_name(info['branch'])}", check=False)
    info["appliedSha"] = applied
    info["appliedTree"] = git(repo, "rev-parse", f"{applied}^{{tree}}", check=False) if applied else ""
    info["matchesApplied"] = bool(applied) and info["treeSha"] == info["appliedTree"]
    return info


def capture_wip(repo: Path, node_id: str) -> dict[str, Any]:
    """Record uncommitted work as refs/heads/wip/<node>/<branch> without touching the working tree."""
    snap = snapshot(repo, node_id)
    branch = snap["branch"]
    if branch == "HEAD":
        raise RepoOpError("detached HEAD: check out a branch before handing off")
    out = {"branch": branch, "head": snap["headFull"], "wipSha": "", "wipRef": "", "dirty": snap["dirty"]}
    if snap["dirty"]:
        tree = snap["treeSha"]
        msg = f"orch WIP from {node_id} on {branch} at {time.strftime('%Y-%m-%d %H:%M:%S')}"
        env = {"GIT_AUTHOR_NAME": "llm-orchestration", "GIT_AUTHOR_EMAIL": "orch@localhost",
               "GIT_COMMITTER_NAME": "llm-orchestration", "GIT_COMMITTER_EMAIL": "orch@localhost"}
        wip = git(repo, "commit-tree", tree, "-p", snap["headFull"], "-m", msg, env=env)
        ref = f"wip/{_ref_name(node_id)}/{_ref_name(branch)}"
        git(repo, "update-ref", f"refs/heads/{ref}", wip)
        out.update({"wipSha": wip, "wipRef": ref})
    out["markSha"] = out["wipSha"] or snap["headFull"]
    out["treeSha256"] = snap.get("treeSha256", "")
    return out


def mark_handed_off(repo: Path, branch: str, sha: str) -> None:
    """Remember the exact state we handed away, so we can later tell 'untouched since' from 'edited since'."""
    git(repo, "update-ref", f"refs/orch/applied/{_ref_name(branch)}", sha)


def push_to_origin(repo: Path, branch: str, wip_ref: str) -> dict[str, Any]:
    """Best-effort GitHub copy: normal branch only fast-forward; our own wip ref may be overwritten."""
    res: dict[str, Any] = {"branch": None, "wip": None}
    if not git_ok(repo, "remote", "get-url", "origin"):
        return {"skipped": "no origin remote"}
    try:
        git(repo, "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}", timeout=120)
        res["branch"] = "pushed"
    except (RepoOpError, subprocess.TimeoutExpired) as exc:
        res["branch"] = f"not pushed: {str(exc)[:200]}"
    if wip_ref:
        try:
            git(repo, "push", "--force", "origin", f"refs/heads/{wip_ref}:refs/heads/{wip_ref}", timeout=120)
            res["wip"] = "pushed"
        except (RepoOpError, subprocess.TimeoutExpired) as exc:
            res["wip"] = f"not pushed: {str(exc)[:200]}"
    return res


def make_bundle(repo: Path, refs: list[str], basis: str = "") -> bytes:
    with tempfile.TemporaryDirectory() as td:
        f = str(Path(td) / "x.bundle")
        args = ["bundle", "create", f, *refs]
        if basis and git_ok(repo, "cat-file", "-e", f"{basis}^{{commit}}"):
            args.append(f"^{basis}")
        try:
            git(repo, *args, timeout=300)
        except RepoOpError as exc:
            if "empty bundle" not in str(exc).lower() and "refusing to create empty" not in str(exc).lower():
                raise
            git(repo, "bundle", "create", f, *refs, timeout=300)
        return Path(f).read_bytes()


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", a, b)


def apply_handoff(
    repo: Path,
    node_id: str,
    from_node: str,
    bundle: bytes,
    branch: str,
    head: str,
    wip_sha: str,
    expected_bundle_sha256: str = "",
    expected_tree_sha256: str = "",
) -> dict[str, Any]:
    """Make this working copy match the other node's (branch, commits, uncommitted work) and verify SHA-256."""
    bundle_hash = bytes_sha256(bundle)
    if expected_bundle_sha256 and bundle_hash != expected_bundle_sha256:
        raise RepoOpError(f"bundle SHA-256 mismatch: expected {expected_bundle_sha256[:12]}, got {bundle_hash[:12]}")
    with tempfile.TemporaryDirectory() as td:
        bf = Path(td) / "in.bundle"
        bf.write_bytes(bundle)
        git(repo, "fetch", "--no-tags", str(bf), f"+refs/heads/*:refs/orch/from/{_ref_name(from_node)}/*", timeout=300)

    if not git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        raise RepoOpError("handoff bundle did not contain the other node's HEAD")
    snap = snapshot(repo, node_id)
    actions: list[str] = []
    cur0 = snap["headFull"]
    if snap["branch"] == branch and cur0 and cur0 != head and not is_ancestor(repo, cur0, head) and not is_ancestor(repo, head, cur0):
        saved = capture_wip(repo, node_id) if snap["dirty"] else {}
        return {"status": "conflict_diverged", "message": (
            f"{branch} has diverged between the machines (both have their own commits). Nothing was merged. "
            f"The other machine's branch is at refs/orch/from/{from_node}/{branch}."
            + (f" This machine's uncommitted work is saved as {saved['wipRef']}." if saved.get("wipRef") else "")), "actions": actions}

    if snap["dirty"]:
        if not snap["matchesApplied"]:
            mine = capture_wip(repo, node_id)
            return {"status": "conflict_both_dirty", "message": (
                f"Both machines have uncommitted changes in {repo.name}. Nothing was overwritten. "
                f"This machine's work is saved as {mine['wipRef']}; the other machine's as wip/{from_node}/{branch}."),
                "localWip": mine}
        git(repo, "stash", "push", "-u", "-m", f"orch-handoff-backup {time.strftime('%Y-%m-%d %H:%M:%S')}", timeout=120)
        actions.append("stashed the old (already handed-off) working copy as a backup")

    if snap["branch"] != branch:
        if git_ok(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
            git(repo, "checkout", branch)
        else:
            git(repo, "checkout", "-b", branch, head)
        actions.append(f"checked out {branch}")

    cur = git(repo, "rev-parse", "HEAD")
    if cur != head:
        if is_ancestor(repo, cur, head):
            git(repo, "merge", "--ff-only", head, timeout=120)
            actions.append(f"fast-forwarded {branch} to {head[:8]}")
        elif is_ancestor(repo, head, cur):
            if wip_sha:
                return {"status": "conflict_local_ahead", "message": (
                    f"This machine has commits on {branch} the other doesn't, and the other has uncommitted work on an older commit. "
                    f"Its work is saved as wip/{from_node}/{branch}; nothing was changed here."), "actions": actions}
            actions.append("this machine is already ahead; nothing to bring over")
        else:
            return {"status": "conflict_diverged", "message": (
                f"{branch} has diverged between the machines (both have their own commits). Nothing was merged. "
                f"The other machine's branch is at refs/orch/from/{from_node}/{branch}."), "actions": actions}

    if wip_sha:
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--binary", head, wip_sha], capture_output=True, timeout=120).stdout
        if diff.strip():
            p = subprocess.run(["git", "-C", str(repo), "apply", "--whitespace=nowarn"], input=diff, capture_output=True, timeout=120)
            if p.returncode != 0:
                return {"status": "conflict_apply", "message": f"Could not apply the uncommitted work: {p.stderr.decode(errors='replace')[:300]}", "actions": actions}
            actions.append("restored the other machine's uncommitted changes")
    git(repo, "update-ref", f"refs/orch/applied/{_ref_name(branch)}", wip_sha or head)
    dest_sha256 = tree_fingerprint(repo)
    if expected_tree_sha256 and dest_sha256 != expected_tree_sha256:
        return {
            "status": "conflict_sha256_mismatch",
            "actions": actions,
            "branch": branch,
            "head": head,
            "bundleSha256": bundle_hash,
            "sourceSha256": expected_tree_sha256,
            "destSha256": dest_sha256,
            "sha256Verified": False,
            "message": f"Post-sync SHA-256 mismatch on {repo.name}: expected {expected_tree_sha256[:12]}, got {dest_sha256[:12]}",
        }
    return {
        "status": "handed_off",
        "actions": actions,
        "branch": branch,
        "head": head,
        "bundleSha256": bundle_hash,
        "sourceSha256": expected_tree_sha256,
        "destSha256": dest_sha256,
        "sha256Verified": bool(dest_sha256),
    }
