#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_PATH="$REPO_DIR/src-tauri/target/release/llm-orchestration"
ICON_SRC="$REPO_DIR/src-tauri/icons/128x128.png"
ICON_DIR="$HOME/.local/share/icons/hicolor/128x128/apps"
ICON_DEST="$ICON_DIR/llm-orchestration.png"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/llm-orchestration.desktop"

if [[ ! -x "$BIN_PATH" ]] || [[ "${1:-}" == "--rebuild" ]]; then
  echo "==> Building Tauri native release binary..."
  cd "$REPO_DIR"
  # A private overlay (identifier, CSP for your own hosts) is applied when present.
  extra=()
  [[ -f "$REPO_DIR/src-tauri/tauri.private.conf.json" ]] && extra=(--config "$REPO_DIR/src-tauri/tauri.private.conf.json")
  npm run tauri build -- --no-bundle "${extra[@]}"
fi

if [[ ! -x "$BIN_PATH" ]]; then
  echo "ERROR: Expected binary not found at $BIN_PATH" >&2
  exit 1
fi

mkdir -p "$ICON_DIR" "$APP_DIR"
cp "$ICON_SRC" "$ICON_DEST"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=LLM-Orchestration
Comment=Multi-Node AI Agent Orchestrator, Repos & Vault Workbench
Exec=$BIN_PATH
Icon=llm-orchestration
Terminal=false
Type=Application
Categories=Development;Utility;
StartupWMClass=llm-orchestration
EOF

chmod +x "$DESKTOP_FILE"

if [[ -d "$HOME/Desktop" ]]; then
  cp "$DESKTOP_FILE" "$HOME/Desktop/LLM-Orchestration.desktop"
  chmod +x "$HOME/Desktop/LLM-Orchestration.desktop"
  echo "==> Created desktop shortcut: $HOME/Desktop/LLM-Orchestration.desktop"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" || true
fi

echo "==> Installed app menu launcher: $DESKTOP_FILE"
echo "==> Binary: $BIN_PATH"
