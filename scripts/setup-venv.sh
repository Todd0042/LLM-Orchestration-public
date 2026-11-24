#!/usr/bin/env bash
# Create/refresh the daemon's Python environment and point the systemd unit at it.
# Uses the system packages too (--system-site-packages); adds only what's in requirements.txt.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.local/share/llm-orchestration/venv"
[ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$REPO/requirements.txt"
UNIT="$HOME/.config/systemd/user/llm-orchestration.service"
if [ -f "$UNIT" ] && ! grep -q "^ExecStart=$VENV/bin/python -m server.app" "$UNIT"; then
  cp "$UNIT" "$UNIT.pre-venv.bak"
  sed -i "s#^ExecStart=.*#ExecStart=$VENV/bin/python -m server.app#" "$UNIT"
  systemctl --user daemon-reload
  echo "unit now runs $VENV/bin/python (restart llm-orchestration to apply)"
fi
"$VENV/bin/python" -c "import webauthn, fastapi; print('venv ok: webauthn', webauthn.__version__)"
