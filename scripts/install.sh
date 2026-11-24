#!/usr/bin/env bash
# One-time setup of LLM-Orchestration on this machine:
#   1. llm-orchestration.env from the example (if you don't have one yet)
#   2. the Python environment (scripts/setup-venv.sh)
#   3. the web UI (npm ci && npm run build)
#   4. the systemd user service, started now and on every login   (skip with --no-service)
#   5. the authenticator secret for the 6-digit sign-in code       (scripts/new-totp.py)
# Safe to re-run: existing settings, secrets and the env file are kept.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.local/share/llm-orchestration/venv"
service=1
[[ "${1:-}" == --no-service ]] && service=0
say() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }

cd "$REPO"
if [[ ! -f llm-orchestration.env ]]; then
  cp llm-orchestration.env.example llm-orchestration.env
  say "Created llm-orchestration.env from the example: edit it, then re-run or restart the service."
fi

say "Python environment"
bash scripts/setup-venv.sh

say "Web UI"
command -v npm >/dev/null || { echo "npm is required (Node.js 20+)" >&2; exit 1; }
npm ci --no-audit --no-fund
npm run build

if ((service)); then
  say "systemd user service"
  unit="$HOME/.config/systemd/user/llm-orchestration.service"
  mkdir -p "$(dirname "$unit")"
  sed "s#@REPO_DIR@#$REPO#" deploy/llm-orchestration.service > "$unit"
  systemctl --user daemon-reload
  systemctl --user enable --now llm-orchestration.service
fi

say "Sign-in code"
"$VENV/bin/python" scripts/new-totp.py

port="$(grep -E '^LLM_ORCH_PORT=' llm-orchestration.env 2>/dev/null | cut -d= -f2)"
say "Done. Open http://127.0.0.1:${port:-8790} on this machine."
((service)) || say "Start it yourself with: $VENV/bin/python -m server.app"
