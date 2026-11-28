# LLM-Orchestration: Architecture & Operations

## 1. What it is for

Two always-on machines (or one), used interchangeably and mostly remotely. A typical pair:

| | Desktop (`desktop`) | Laptop (`laptop`) |
|---|---|---|
| Role | **hub**: the orchestrator you talk to, public tunnel, sites | **worker** + **standby** for the sites |
| Why | never moves, bigger GPU, runs your self-hosted sites | portable, takes over the sites when the desktop is down |
| Repos | e.g. `~/Documents/GitHub` | e.g. `~/GitHub` |

A single machine works too: with no peers configured, replication, backups and failover simply stay idle.

Capabilities are **measured** (`GET /api/node/profile`: RAM, VRAM, disk, battery, load), never inferred
from the names "laptop"/"desktop". Peers are a list in settings; each peer may have several URLs
(LAN, later Tailscale), tried in order.

## 2. Process layout (`server/`)

One process per machine (`python3 -m server.app`, user unit `llm-orchestration.service`), three listeners:

| Listener | Who | Auth |
|---|---|---|
| `bindHost:8790` | local apps (Tauri, MCP bridge, hooks) and LAN machines | loopback: trusted (still origin/host/CSRF-checked). LAN: node token or code session |
| `127.0.0.1:8791` | your tunnel (e.g. Cloudflare Tunnel) only; its host name goes in `security.publicHosts` | **always** the 6-digit-code session. The node token is refused here |
| `127.0.0.1:8792` | failover "back soon" page | none (static) |

Modules:

| Module | Responsibility |
|---|---|
| `app.py` | app factory, security gate middleware, `/ws/events`, SPA, listeners |
| `context.py` | `AppContext`: owns every service; background loops |
| `security.py` | zones, origin/host allowlists, file-read roots, signed media URLs, WS tickets, protected settings |
| `core.py` | broadcaster, audit log, approvals (+ read-only command classifier), background jobs |
| `nodes.py` | peer registry, health (multi-URL), local capability profile |
| `llm.py` | model routing (explicit fallback with reason), completions, GPU turns via gpu-scheduler |
| `agents.py` | AGY/Claude dispatch: local or peer, path mapping between machines, timeouts, never silent fallback |
| `orch_terminal.py` | the agent terminals: tmux sessions per repo, the baton, branch hooks, handoff, merge (§4) |
| `orchestrator.py` | the old chat turn (tool loop), the shared tool layer and briefing, peer relay |
| `repo_ops.py` / `repo_sync.py` | non-destructive git primitives / handoff engine, preflight, active device |
| `replication.py` | two-way replication of conversations, repo activity and the vault |
| `monitor.py` | watchdog, phone alerts (ntfy), SQLite backups to the other node, failover controller |
| `routes/*` | HTTP routes per area (auth, system, repos, chats, terminal, orch-terminal, ops, internal) |
| `cli_agents.py`, `history_readers.py`, `pty_runner.py`, `gpu_and_media.py`, `orch_tools.py`, `mcp_bridge.py` | CLI runners, session readers, PTY + sudo askpass, ComfyUI, tool schemas, MCP stdio bridge |
| `plugins/` | Optional plugins: each module with `build(ctx)` and `FEATURE` is loaded at start-up and listed at `GET /api/features`; the UI shows a plugin (`src/plugins/<id>/`) only when its server side loaded |

Database: `~/.local/share/llm-orchestration/orchestration.db` (0600), versioned migrations in `db.py`.

## 3. Security model

- **Zones** are decided per request and can't be relaxed by settings: tunnel listener **or** any Cloudflare
  header ⇒ `tunnel`; loopback ⇒ `local`; anything else ⇒ `lan`.
- **The only human login is the 6-digit code** (RFC 6238, replay-protected, per-IP + global lockout). The
  session cookie is HttpOnly, Secure + SameSite=Strict on the tunnel, idle timeout 1 h.
- **Browsers can't be used against it:** foreign `Origin` ⇒ 403 (allowed: same origin, Tauri, the public
  host, Vite dev); unknown `Host` names ⇒ 421 (stops DNS rebinding; raw IPs, the machine's names and
  `*.ts.net` are allowed); every state-changing request needs `X-Orch-Client` (or the node token).
- **The node token stays between machines:** never sent to browsers, never in URLs (WebSocket uses one-time
  tickets, media uses signed expiring links), not accepted over the tunnel, rotatable
  (`POST /api/admin/rotate-token`, local only, pushes to peers).
- **Protected settings** (auth, security, node, peers, tools dir, model server URL, CLI permission flags,
  shell, approvals, services, failover, backups, the agent terminals' commands) change only from the machine
  itself or a peer machine.
- **Agent terminals are watch-only over the tunnel:** starting, typing, handing off and merging are refused
  there, and the browser's `tmux attach` is read-only (`-r`). Drive Claude remotely through its Remote
  Control instead, which uses your Anthropic login. Every terminal connection is in the activity log with
  its zone and whether it could type.
- **File reads** are limited to the repo dir, vault, artifacts, AGY/Claude session dirs and backups
  (symlinks resolved first).
- **Model-chosen shell commands** need an Approve/Deny click unless they're a single read-only command
  (`git status/log/diff/fetch…`, `ls`, `df`, …; no shell metacharacters).
- **sudo** passwords go UI → memory → askpass socket only; never logged, stored, or broadcast.
- **Activity log** (`/api/audit`, Hub → Activity Log): logins, terminal commands, agent turns, approvals,
  settings changes, handoffs, restarts, alerts, with the zone they came from.

## 4. Agent terminals (the orchestrator card)

The orchestrator is **three real terminals per repo**, so you see everything an agent does (tool calls,
"Compacting conversation…", `/context`) instead of a summary of it:

| Slot | Runs | Busy / idle from |
|---|---|---|
| **Claude** | `claude --dangerously-skip-permissions --remote-control <node>-<repo> --session-id <uuid> --settings <hooks> --mcp-config <orch tools> --append-system-prompt-file <briefing>` | its `SessionStart` / `UserPromptSubmit` / `Stop` hooks, which post to `/api/orch-terminal/hook` with a per-card secret; an interrupt from the card, or a still screen for `claudeStaleBusySec`, also counts as idle (Claude fires no hook when interrupted) |
| **AGY** | `agy --dangerously-skip-permissions` | the screen: busy while its newest 15 lines keep changing (`orchTerminal.idleAfterSec`) |
| **Local** | `opencode --auto` (`--standalone` on opencode 2.x) with its own `OPENCODE_CONFIG`: this machine's model server and model, the orch tools over MCP | the screen, like AGY |

Implementation: `server/orch_terminal.py` (`OrchTerminals`, `Tmux`, `AttachedTerminal`),
`server/routes/orch_terminal_routes.py`, UI in `src/components/orch/`.

- **tmux, on its own socket** (`<data>/runtime/orch-tmux.sock`, config `orch-tmux.conf`, no prefix key,
  `remain-on-exit`): the agents keep running when the browser closes or this server restarts. The first
  session starts the tmux server under `systemd-run --user --scope` so restarting
  `llm-orchestration.service` doesn't kill it (`orchTerminal.detachWithSystemd`). Attach from a real
  terminal with `tmux -S <socket> attach -t orch-<card>-<slot>`, or the card's "Open in Alacritty".
- **Browser view:** `/ws/orch-terminal/{card}/{slot}?ticket=…` runs its own `tmux attach` in a PTY per
  browser; screen bytes go down, `{"t":"i"}` keystrokes and `{"t":"r"}` resizes go up. xterm.js is loaded
  only when a terminal is shown. An ended agent shows Resume / New session, never a bare shell.
- **The baton** (`orch_cards.holder`): only the holder's terminal takes typing; the server drops keystrokes
  for the others before they reach tmux. A handoff is refused while the holder is busy (interrupt it from
  the card first). It asks the holder to update `.orch/context.md` (waiting for Claude's `Stop` hook or
  the screen to go quiet), appends a line to the handoff log, copies the notes to the vault, starts the next
  agent if needed, moves the baton, and types the handoff message into the next terminal where you can see
  it. If the holder is out of usage, the notes update is skipped and the log says why.
- **Out of usage:** a usage-limit message on an agent's screen marks it out until the reset time it states
  (and in `AgentAvailability`, which the old chat uses). The card offers a handoff; it never makes one.
- **Branch-only work:** opening a card switches the repo to `orchestrator-modified` (refused if the
  working tree is dirty). The agents' environment sets `GIT_CONFIG_COUNT/KEY_0/VALUE_0` so their git uses
  `<data>/runtime/orch-git-hooks`: `pre-commit` and `pre-merge-commit` refuse any other branch, `pre-push`
  refuses any other ref, and every hook then runs the repo's own hook (`core.hooksPath` or `.git/hooks`).
  Your own terminals are unaffected. `--no-verify` would get past it, so main can also be protected on
  GitHub (the card's button; public repos only on GitHub's free plan, admins can still push).
- **Merge to main** (the card's button, refused while an agent is busy): archives the notes to
  `LLM-Vault/orchestrator/<repo>/archive/`, merges with `--no-ff` leaving `.orch/` out of main, pushes main,
  deletes the branch (locally and on GitHub) and recreates it from the new main with fresh notes.
- **Shared notes:** `.orch/context.md` (Goal · Branch · Done so far · Decisions · Next steps · Handoff log)
  lives on the branch; `LLM-Vault/orchestrator/<repo>/context.md` is a read-only copy for the other machine.

The previous chat orchestrator (`orchestrator.py`'s turn loop) is still one tab away ("Old chat") until it's
removed; its tool layer (`call_tool`, approvals, `briefing()`) is shared with the terminals.

## 5. Repo sync (the reason this exists)

- `repo_activity` = the **active device** per repo (replicated).
- **Preflight** runs before work starts in a repo: every agent turn, every UI terminal command in a repo,
  the MCP tool `orch_repo_preflight`, and the Claude Code **SessionStart hook**
  (`scripts/hooks/orch-preflight.sh`, prints one status line into the session).
- **Handoff** (other machine → this one):
  1. capture uncommitted work (tracked + untracked, honoring `.gitignore`) as a commit via a temporary
     index into `refs/heads/wip/<node>/<branch>`; the working tree and index are untouched;
  2. push the branch (fast-forward only) and the wip ref to GitHub as a backup;
  3. send both directly as a git bundle; on the receiving side fast-forward the branch and re-apply the
     uncommitted changes as uncommitted changes;
  4. mark the handed-off state on the sender, so "untouched since" is distinguishable from "edited since".
- **Never destructive:** a dirty working copy is only replaced if it's byte-identical to what it handed
  away, and even then it's stashed first. Both-dirty, diverged commits, or patches that don't apply stop
  with a report, an alert, and a wip ref on each side. Nothing is merged, reset or force-pushed (except our
  own `wip/` refs).
- Hub → **Repo Sync** shows every repo on every machine, the active device, recent handoffs, and a one-click
  "bring work to …".

## 6. One conversation on both machines

Every node pulls from every online peer: `chats`, `messages`, `artifacts`, `repo_activity` by a local
change counter (`seq`, maintained by triggers), so no clocks are trusted for ordering. Messages are
immutable (unique ids); chats and activity are last-writer-wins. If the hub is down, the laptop answers
with its own models (the reply says it fell back and why) and everything merges on reconnect.

The **vault** syncs both ways with a three-way compare against the last-agreed hash: one-sided changes copy,
deletions propagate, both-sided edits keep the newer file and save the other as
`name.conflict-<node>-<hash8>.ext` (identical on both machines).

## 7. GPU sharing (optional)

If several apps share one GPU, model calls can take turns through a separate GPU-scheduler service
(`paths.gpuSchedulerUrl`, `LLM_ORCH_USE_GPU_SCHEDULER=1`): each call asks for a turn
(`needs=ollama:<model>`, app `llm_orchestration`) and releases it afterwards. Without a scheduler
(the default for new installs) model calls go straight to the model server.

## 8. Sites: watchdog, alerts, backups, failover

- `settings.services` is the one list of sites/services (see `deploy/examples/services.example.json` and its field guide).
- Watchdog: check every 30 s; restart only after 3
  consecutive failures; exponential backoff; at most 4 restarts/hour, then **gives up and alerts**; blue/green
  units restart one slot at a time; recovery is reported. Hub → **Sites & Watchdog**.
- Alerts: dashboard + activity log + phone via ntfy (`alerts.ntfyUrl`), rate-limited per subject.
- Backups: consistent SQLite snapshots (SQLite backup API), gzipped, sent to the other machine's
  `<backupsDir>/<node>/<service>/` (default `~/Backups/llm-orchestration`) every 6 h, keeping 14.
- Failover (laptop, `failover.role=standby`): if the desktop is unreachable for 2 min **and** the public
  probe doesn't answer as the desktop **and** the laptop has internet, start the failover unit (`failover.connectorUnit`, e.g. `llm-orchestration-failover.target`)
  (second connectors for both tunnels: sites show the "back soon" page, the orchestrator stays reachable);
  stop it once the desktop has been back for 3 min.

## 9. Clients

- Web UI (Vite/React) served by the daemon; Tauri app embeds `dist/` and talks to the hub with the token
  from `~/.config/llm-orchestration/node_token` (CSP set in `tauri.conf.json`).
- Long work (turns, agent runs, handoffs) are **jobs**: the request returns a `jobId`; progress streams over
  `/ws/events`; `GET /api/jobs/{id}` for state.

## 10. Files

| What | Where |
|---|---|
| Settings / node token | `~/.config/llm-orchestration/settings.json` (0600), `node_token` (0600) |
| DB, artifacts, runtime | `~/.local/share/llm-orchestration/` (agent terminals: `runtime/orch-tmux.sock`, `orch-git-hooks/`) |
| TOTP secret | `<auth.totpStateDir>/totp_secret`, default `~/.local/share/llm-orchestration/totp_secret` (created by `scripts/new-totp.py`) |
| Vault | `~/LLM-Vault/` (both machines, synced) |
| Backups | `~/Backups/llm-orchestration/` (on the receiving machine; `paths.backupsDir`) |
| Units | `deploy/llm-orchestration.service`; tunnel and failover templates in `deploy/examples/` |
| Tunnel configs | e.g. `~/.cloudflared/orchestrator.yml` (primary), `failover-*.yml` (standby) |
