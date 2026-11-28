#!/usr/bin/env bash
# Can this machine's local model actually work as the orchestrator card's Local agent?
#
# Makes a throwaway repo in your GitHub folder, opens an agent card for it, starts the Local slot
# (opencode + the configured local model) and asks it to create and commit a file. PASS means the model
# used its tools and the commit exists; FAIL usually means it only wrote out instructions, so pick
# another model (orchTerminal.localModel). It types straight into opencode's terminal, so Claude and AGY
# are never prompted and use no usage. Everything it made is removed at the end.
#
# Run on the machine to check:   scripts/check-local-agent.sh [timeout-seconds, default 600: a local model reads ~18k tokens of opencode instructions first]
set -euo pipefail

API="http://127.0.0.1:${LLM_ORCH_PORT:-8790}/api/orch-terminal"
SETTINGS="${LLM_ORCH_CONFIG_DIR:-$HOME/.config/llm-orchestration}/settings.json"
SOCK="${LLM_ORCH_DATA_DIR:-$HOME/.local/share/llm-orchestration}/runtime/orch-tmux.sock"
TIMEOUT="${1:-600}"
H=(-H "X-Orch-Client: 1" -H "Content-Type: application/json")

for cmd in curl jq git tmux; do command -v "$cmd" >/dev/null || { echo "needs $cmd" >&2; exit 2; }; done
curl -sf -m 3 "http://127.0.0.1:${LLM_ORCH_PORT:-8790}/api/health" >/dev/null || { echo "LLM-Orchestration isn't running here" >&2; exit 2; }

GH="$(jq -r '.paths.githubDir // empty' "$SETTINGS")"; GH="${GH:-$HOME/GitHub}"; GH="${GH/#\~/$HOME}"
VAULT="$(jq -r '.paths.vaultDir // empty' "$SETTINGS")"; VAULT="${VAULT:-$HOME/LLM-Vault}"; VAULT="${VAULT/#\~/$HOME}"
NAME="orch-localcheck-$(date +%s)"
REPO="$GH/$NAME"
CID=""

cleanup() {
  [[ -n "$CID" ]] && curl -s -m 30 -X DELETE "${H[@]}" "$API/cards/$CID" >/dev/null || true
  [[ -n "$NAME" && -d "$REPO" && "$REPO" == "$GH/orch-localcheck-"* ]] && rm -rf -- "$REPO"
  [[ -d "$VAULT/orchestrator/$NAME" ]] && rm -rf -- "$VAULT/orchestrator/$NAME"
  rmdir "$VAULT/orchestrator" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> Throwaway repo $REPO"
mkdir "$REPO"
git -C "$REPO" init -q -b main
echo "# local agent check" > "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" -c user.name=check -c user.email=check@localhost commit -q -m init

echo "==> Opening a card and starting the Local agent"
CID="$(curl -sf -m 60 "${H[@]}" "$API/cards" -d "{\"repo\":\"$NAME\"}" | jq -r .id)"
curl -sf -m 60 -X POST "${H[@]}" "$API/cards/$CID/slots/local/start" >/dev/null
SESSION="=orch-$CID-local:"
sleep 12
model="$(jq -r .model "${LLM_ORCH_DATA_DIR:-$HOME/.local/share/llm-orchestration}/runtime/orch-opencode.json")"
echo "    model: $model"

echo "==> Asking it to create and commit hello.txt (up to ${TIMEOUT}s)"
tmux -S "$SOCK" send-keys -t "$SESSION" -l "Create a file named hello.txt containing the word hi, then git add it and commit it with the message add hello."
sleep 0.5
tmux -S "$SOCK" send-keys -t "$SESSION" Enter

result=FAIL
for ((t = 0; t < TIMEOUT; t += 5)); do
  sleep 5
  if git -C "$REPO" log --all --format=%s | grep -qi 'add hello'; then result=PASS; break; fi
done

echo "==> Last lines of its screen:"
tmux -S "$SOCK" capture-pane -p -J -t "$SESSION" | grep -v '^[[:space:]]*$' | tail -15 | cut -c1-160
echo
if [[ $result == PASS ]]; then
  echo "PASS: $model used its tools and committed (on $(git -C "$REPO" log --all --format=%D -1 --grep='add hello' | sed 's/HEAD -> //'))."
else
  echo "FAIL: no commit after ${TIMEOUT}s. $model probably can't drive opencode's tools; try another model in orchTerminal.localModel."
fi
[[ $result == PASS ]]
