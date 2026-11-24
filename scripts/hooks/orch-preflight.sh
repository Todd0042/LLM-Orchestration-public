#!/usr/bin/env bash
# Claude Code SessionStart hook: before a session works in a repo, ask the local
# LLM-Orchestration daemon to bring over any newer/uncommitted work from the other machine.
# Prints one status line (added to the session's context). Never blocks the session.
set -u
input="$(cat 2>/dev/null || true)"
cwd="$(printf '%s' "$input" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("cwd",""))
except Exception: print("")' 2>/dev/null)"
[ -n "$cwd" ] || cwd="$PWD"
cfg="${LLM_ORCH_CONFIG_DIR:-$HOME/.config/llm-orchestration}"
tok="$(cat "$cfg/node_token" 2>/dev/null || true)"
port="$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]))["node"].get("port",8790))
except Exception: print(8790)' "$cfg/settings.json" 2>/dev/null)"
body="$(python3 -c 'import json,sys; print(json.dumps({"cwd": sys.argv[1], "source": "claude-session"}))' "$cwd")"
out="$(curl -fsS -m 90 -H "X-Node-Token: $tok" -H 'Content-Type: application/json' \
  -d "$body" "http://127.0.0.1:${port}/api/repos/preflight" 2>/dev/null)" || exit 0
msg="$(printf '%s' "$out" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("message",""))
except Exception: print("")' 2>/dev/null)"
[ -n "$msg" ] && echo "[repo sync] $msg"
exit 0
