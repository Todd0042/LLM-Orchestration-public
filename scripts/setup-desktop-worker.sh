#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# This machine's settings (copy llm-orchestration.env.example to llm-orchestration.env).
if [[ -f "$REPO_DIR/llm-orchestration.env" ]]; then set -a; . "$REPO_DIR/llm-orchestration.env"; set +a; fi
GITHUB_DIR="${LLM_ORCH_GITHUB_DIR:-$(dirname "$REPO_DIR")}"
HUB_IP="${1:-${LLM_ORCH_HUB_IP:-}}"
LAN_CIDR="${LLM_ORCH_LAN_CIDR:-}"
HUB_SSH_USER="${LLM_ORCH_HUB_SSH_USER:-$USER}"
if [[ -z "$HUB_IP" ]]; then
  echo "Usage: $0 <hub-ip>   (or set LLM_ORCH_HUB_IP in llm-orchestration.env)" >&2
  exit 1
fi
CFG_DIR="$HOME/.config/llm-orchestration"
TOKEN_FILE="$CFG_DIR/node_token"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/llm-orchestration.service"

echo "==> LLM-Orchestration Desktop Worker Setup"
echo "    Repo dir   : $REPO_DIR"
echo "    GitHub dir : $GITHUB_DIR"
echo "    Hub        : http://${HUB_IP}:8790"
echo

# 1. Python runtime dependencies
echo "[1/5] Checking Python dependencies (fastapi, uvicorn, httpx, pydantic, websockets)..."
if ! python3 -c "import fastapi, uvicorn, httpx, pydantic, websockets" 2>/dev/null; then
  echo "Installing missing Python packages..."
  python3 -m pip install --user fastapi uvicorn httpx pydantic websockets \
    || python3 -m pip install --user --break-system-packages fastapi uvicorn httpx pydantic websockets
fi

# 2. Shared LAN X-Node-Token
mkdir -p "$CFG_DIR"
chmod 700 "$CFG_DIR"
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "[2/5] Shared node_token not found at $TOKEN_FILE."
  echo "Choose how to provide it from the hub (~/.config/llm-orchestration/node_token):"
  echo "  1) Copy via scp from ${HUB_SSH_USER}@${HUB_IP} (requires sshd on the hub)"
  echo "  2) Paste token directly"
  read -r -p "Selection [1/2]: " choice
  if [[ "$choice" == "1" ]]; then
    scp "${HUB_SSH_USER}@${HUB_IP}:~/.config/llm-orchestration/node_token" "$TOKEN_FILE"
  else
    read -r -s -p "Paste node_token (hidden input): " pasted_tok
    echo
    if [[ -z "${pasted_tok// }" ]]; then
      echo "ERROR: Empty token aborted." >&2
      exit 1
    fi
    printf '%s\n' "$pasted_tok" > "$TOKEN_FILE"
  fi
fi
chmod 600 "$TOKEN_FILE"
echo "    Using shared token: $TOKEN_FILE (0600)"

# 3. Optional MCP registration for AGY
if command -v agy >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/agy" ]]; then
  echo "[3/5] Registering stdio MCP bridge in AGY..."
  AGY_BIN="$(command -v agy || echo "$HOME/.local/bin/agy")"
  "$AGY_BIN" mcp add llm-orchestration -- /usr/bin/python3 "$REPO_DIR/server/mcp_bridge.py" || true
else
  echo "[3/5] Skipping AGY MCP registration (agy not found)."
fi

# 4. Firewall rule for LAN port 8790
if [[ -z "$LAN_CIDR" ]]; then
  echo "[4/5] LLM_ORCH_LAN_CIDR not set (e.g. 192.0.2.0/24); skipping the firewall rule."
elif command -v ufw >/dev/null 2>&1; then
  echo "[4/5] Allowing $LAN_CIDR -> 8790/tcp in the firewall..."
  sudo ufw allow from "$LAN_CIDR" to any port 8790 proto tcp || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  echo "[4/5] Allowing $LAN_CIDR -> 8790/tcp in the firewall..."
  sudo firewall-cmd --permanent --add-rich-rule="rule family=\"ipv4\" source address=\"$LAN_CIDR\" port protocol=\"tcp\" port=\"8790\" accept" || true
  sudo firewall-cmd --reload || true
elif grep -qi nixos /etc/os-release 2>/dev/null; then
  echo "    NixOS detected: ensure networking.firewall.allowedTCPPorts = [ 8790 ]; is in your Nix config."
else
  echo "    No ufw/firewalld detected; ensure TCP 8790 is reachable from $LAN_CIDR."
fi

# 5. User systemd service
echo "[5/5] Installing and starting systemd --user service..."
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LLM-Orchestration Desktop Worker (0.0.0.0:8790)
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=LLM_ORCH_NODE_ID=desktop
Environment=LLM_ORCH_ROLE=worker
Environment=LLM_ORCH_PORT=8790
Environment=LLM_ORCH_HUB_URL=http://${HUB_IP}:8790
Environment=LLM_ORCH_GITHUB_DIR=$GITHUB_DIR
ExecStart=/usr/bin/python3 -m server.app
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now llm-orchestration.service
sleep 2

echo
echo "==> Worker Health:"
curl -sS http://127.0.0.1:8790/api/health || echo "(service still starting...)"
echo
echo "==> This machine's LAN IP(s): add http://<IP>:8790 on the hub (Settings -> Hub & LAN Peers):"
ip -4 -brief addr
