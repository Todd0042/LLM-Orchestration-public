"""Read AGY (~/.gemini/antigravity-cli) and Claude Code (~/.claude) sessions, diffs & artifacts."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import sqlite3
from typing import Any


def _unquote(val: Any) -> str:
    if val is None:
        return ""
    if not isinstance(val, str):
        return json.dumps(val, indent=2)
    s = val.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                return decoded
        except Exception:
            pass
    return val


def _guess_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        return "image"
    if ext in (".mp4", ".webm", ".mov", ".mkv"):
        return "video"
    if ext in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
        return "audio"
    if ext in (".md", ".txt", ".pdf", ".html"):
        return "doc"
    return "file"


def _mime_for(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


def _first_prompt(transcript: Path, max_lines: int = 400) -> str:
    """The first thing you typed in an AGY session (streams the file; stops at the first prompt).
    Sessions started by another agent have no typed prompt; their instruction arrives as a system
    message ("... content=<instruction>"), which is returned as "Sub-task: <instruction>"."""
    delegated = ""
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                if step.get("type") == "USER_INPUT" or step.get("source") == "USER_EXPLICIT":
                    text = str(step.get("content") or "")
                    if text.startswith("<USER_REQUEST>") and "</USER_REQUEST>" in text:
                        text = text[len("<USER_REQUEST>"): text.find("</USER_REQUEST>")]
                    return " ".join(text.split())
                if not delegated and step.get("type") == "SYSTEM_MESSAGE" and "content=" in str(step.get("content") or ""):
                    body = str(step["content"]).split("content=", 1)[1].split("</SYSTEM_MESSAGE>", 1)[0]
                    delegated = "Sub-task: " + " ".join(body.split())
    except OSError:
        pass
    return delegated


def _agy_title(title: str | None, transcript: Path, cid: str) -> str:
    """AGY leaves many conversations untitled; use what you asked, not a hex id."""
    if title and title.strip():
        return title.strip()
    first = _first_prompt(transcript)
    if first:
        return first[:70] + ("…" if len(first) > 70 else "")
    return f"Session {cid[:8]}"


def list_agy_chats(agy_dir: Path, node_id: str, limit: int = 80) -> list[dict[str, Any]]:
    db_path = agy_dir / "conversation_summaries.db"
    if not db_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris
               FROM conversation_summaries
               ORDER BY last_modified_time DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        for r in rows:
            cid = r["conversation_id"]
            transcript = agy_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript.is_file():
                continue
            out.append(
                {
                    "id": cid,
                    "source": "agy",
                    "nodeId": node_id,
                    "title": _agy_title(r["title"], transcript, cid),
                    "preview": (r["preview"] or "")[:180],
                    "stepCount": int(r["step_count"] or 0),
                    "workspace": r["workspace_uris"] or "",
                    "updatedAt": str(r["last_modified_time"] or ""),
                }
            )
    except Exception:
        pass
    return out


def read_agy_chat(agy_dir: Path, cid: str, node_id: str) -> dict[str, Any] | None:
    brain_dir = agy_dir / "brain" / cid
    transcript = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.is_file():
        return None

    messages: list[dict[str, Any]] = []
    artifacts_map: dict[str, dict[str, Any]] = {}

    def add_artifact(kind: str, title: str, path: str, extra: dict[str, Any] | None = None) -> None:
        key = f"{kind}:{path}"
        if key not in artifacts_map:
            p = Path(path)
            artifacts_map[key] = {
                "id": f"art_{len(artifacts_map) + 1}",
                "kind": kind,
                "title": title or p.name,
                "path": path,
                "nodeId": node_id,
                "mimeType": _mime_for(p) if p.suffix else "text/plain",
                "meta": extra or {},
            }

    # Collect brain artifacts (plans, scratch, generated media)
    if brain_dir.is_dir():
        for p in sorted(brain_dir.rglob("*")):
            if not p.is_file() or ".system_generated" in p.parts:
                continue
            k = _guess_kind(p)
            add_artifact(k if k != "file" else "doc", p.name, str(p), {"origin": "brain"})

    for raw_line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            step = json.loads(raw_line)
        except Exception:
            continue

        stype = step.get("type")
        src = step.get("source")
        ts = step.get("created_at") or ""

        if stype == "USER_INPUT" or src == "USER_EXPLICIT":
            text = str(step.get("content") or "")
            if text.startswith("<USER_REQUEST>"):
                end = text.find("</USER_REQUEST>")
                if end > 0:
                    text = text[len("<USER_REQUEST>") : end].strip()
            messages.append(
                {
                    "id": f"step_{step.get('step_index', len(messages))}",
                    "role": "user",
                    "senderLabel": "You",
                    "nodeId": node_id,
                    "content": text,
                    "events": [],
                    "createdAt": ts,
                }
            )
            continue

        if stype == "PLANNER_RESPONSE":
            events: list[dict[str, Any]] = []
            thinking = str(step.get("thinking") or "").strip()
            if thinking:
                events.append({"type": "thinking", "text": thinking})
            for tc in step.get("tool_calls") or []:
                name = str(tc.get("name") or "tool")
                args = tc.get("args") or {}
                if name in ("edit_file", "write_file"):
                    fpath = _unquote(args.get("Path"))
                    old_txt = _unquote(args.get("Target"))
                    new_txt = _unquote(args.get("Replacement") if name == "edit_file" else args.get("Content"))
                    events.append(
                        {
                            "type": "diff",
                            "tool": name,
                            "path": fpath,
                            "oldText": old_txt,
                            "newText": new_txt,
                        }
                    )
                    if fpath:
                        add_artifact("diff", Path(fpath).name, fpath, {"oldText": old_txt, "newText": new_txt})
                elif name == "run_command":
                    events.append(
                        {
                            "type": "tool",
                            "tool": name,
                            "summary": _unquote(args.get("toolSummary")) or _unquote(args.get("CommandLine")),
                            "detail": _unquote(args.get("CommandLine")),
                        }
                    )
                else:
                    events.append({"type": "tool", "tool": name, "summary": name, "detail": json.dumps(args)})

            content = str(step.get("content") or "").strip()
            if content or events:
                messages.append(
                    {
                        "id": f"step_{step.get('step_index', len(messages))}",
                        "role": "assistant",
                        "senderLabel": "AGY",
                        "nodeId": node_id,
                        "modelId": str(step.get("model") or "agy"),
                        "content": content,
                        "events": events,
                        "createdAt": ts,
                    }
                )

    return {
        "id": cid,
        "source": "agy",
        "nodeId": node_id,
        "title": messages[0]["content"][:64] if messages else cid,
        "messages": messages,
        "artifacts": list(artifacts_map.values()),
    }


def list_claude_chats(claude_dir: Path, node_id: str, limit: int = 80) -> list[dict[str, Any]]:
    projects_dir = claude_dir / "projects"
    if not projects_dir.is_dir():
        return []
    files = sorted(
        (p for p in projects_dir.rglob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        title = f.stem
        preview = ""
        ws = f.parent.name
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[:25]:
                obj = json.loads(line)
                if obj.get("type") == "user":
                    raw = obj.get("message", {}).get("content")
                    if isinstance(raw, str) and raw.strip():
                        title = raw.strip().splitlines()[0][:64]
                        preview = raw.strip()[:180]
                        ws = obj.get("cwd") or ws
                        break
        except Exception:
            pass
        out.append(
            {
                "id": f"{f.parent.name}__{f.stem}",
                "source": "claude",
                "nodeId": node_id,
                "title": title,
                "preview": preview,
                "workspace": ws,
                "updatedAt": str(int(f.stat().st_mtime * 1000)),
            }
        )
    return out


def read_claude_chat(claude_dir: Path, chat_id: str, node_id: str) -> dict[str, Any] | None:
    if "__" not in chat_id:
        return None
    proj, sess = chat_id.split("__", 1)
    jsonl_path = claude_dir / "projects" / proj / f"{sess}.jsonl"
    if not jsonl_path.is_file():
        return None

    messages: list[dict[str, Any]] = []
    artifacts_map: dict[str, dict[str, Any]] = {}

    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        otype = obj.get("type")
        if otype not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        ts = str(obj.get("timestamp") or "")

        if isinstance(content, str):
            messages.append(
                {
                    "id": str(obj.get("uuid") or f"c_{len(messages)}"),
                    "role": otype,
                    "senderLabel": "You" if otype == "user" else "Claude",
                    "nodeId": node_id,
                    "modelId": str(msg.get("model") or "claude"),
                    "content": content,
                    "events": [],
                    "createdAt": ts,
                }
            )
            continue

        if isinstance(content, list):
            text_parts: list[str] = []
            events: list[dict[str, Any]] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    text_parts.append(str(blk.get("text") or ""))
                elif btype == "thinking":
                    events.append({"type": "thinking", "text": str(blk.get("thinking") or "")})
                elif btype == "tool_use":
                    tname = str(blk.get("name") or "tool")
                    tinput = blk.get("input") or {}
                    if tname in ("Edit", "Write") and isinstance(tinput, dict):
                        fpath = str(tinput.get("file_path") or "")
                        old_s = str(tinput.get("old_string") or "")
                        new_s = str(tinput.get("new_string") if tname == "Edit" else tinput.get("content") or "")
                        events.append({"type": "diff", "tool": tname, "path": fpath, "oldText": old_s, "newText": new_s})
                        if fpath:
                            key = f"diff:{fpath}"
                            artifacts_map[key] = {
                                "id": f"cart_{len(artifacts_map) + 1}",
                                "kind": "diff",
                                "title": Path(fpath).name,
                                "path": fpath,
                                "nodeId": node_id,
                                "mimeType": "text/plain",
                                "meta": {"oldText": old_s, "newText": new_s},
                            }
                    else:
                        summary = str(tinput.get("command") or tinput.get("file_path") or tname) if isinstance(tinput, dict) else tname
                        events.append({"type": "tool", "tool": tname, "summary": summary, "detail": json.dumps(tinput, indent=2)[:1500]})
            joined = "\n".join(p for p in text_parts if p.strip()).strip()
            if joined or events:
                messages.append(
                    {
                        "id": str(obj.get("uuid") or f"c_{len(messages)}"),
                        "role": otype,
                        "senderLabel": "You" if otype == "user" else "Claude",
                        "nodeId": node_id,
                        "modelId": str(msg.get("model") or "claude"),
                        "content": joined,
                        "events": events,
                        "createdAt": ts,
                    }
                )

    return {
        "id": chat_id,
        "source": "claude",
        "nodeId": node_id,
        "title": messages[0]["content"][:64] if messages else sess,
        "messages": messages,
        "artifacts": list(artifacts_map.values()),
    }
