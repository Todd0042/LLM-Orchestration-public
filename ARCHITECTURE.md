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
| `orchestrator.py` | the orchestrator turn (tool loop) and peer relay, run as jobs |
| `repo_ops.py` / `repo_sync.py` | non-destructive git primitives / handoff engine, preflight, active device |
| `replication.py` | two-way replication of conversations, repo activity and the vault |
| `monitor.py` | watchdog, phone alerts (ntfy), SQLite backups to the other node, failover controller |
| `routes/*` | HTTP routes per area (auth, system, repos, chats, terminal, ops, internal) |
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
- **Protected settings** (auth, security, node, peers, tools dir, CLI permission flags, shell, approvals,
  services, failover, backups) change only from the machine itself or a peer machine.
- **File reads** are limited to the repo dir, vault, artifacts, AGY/Claude session dirs and backups
  (symlinks resolved first).
- **Model-chosen shell commands** need an Approve/Deny click unless they're a single read-only command
  (`git status/log/diff/fetch…`, `ls`, `df`, …; no shell metacharacters).
- **sudo** passwords go UI → memory → askpass socket only; never logged, stored, or broadcast.
- **Activity log** (`/api/audit`, Hub → Activity Log): logins, terminal commands, agent turns, approvals,
  settings changes, handoffs, restarts, alerts, with the zone they came from.

## 4. Repo sync (the reason this exists)

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

## 5. One conversation on both machines

Every node pulls from every online peer: `chats`, `messages`, `artifacts`, `repo_activity` by a local
change counter (`seq`, maintained by triggers), so no clocks are trusted for ordering. Messages are
immutable (unique ids); chats and activity are last-writer-wins. If the hub is down, the laptop answers
with its own models (the reply says it fell back and why) and everything merges on reconnect.

The **vault** syncs both ways with a three-way compare against the last-agreed hash: one-sided changes copy,
deletions propagate, both-sided edits keep the newer file and save the other as
`name.conflict-<node>-<hash8>.ext` (identical on both machines).

## 6. GPU sharing (optional)

If several apps share one GPU, model calls can take turns through a separate GPU-scheduler service
(`paths.gpuSchedulerUrl`, `LLM_ORCH_USE_GPU_SCHEDULER=1`): each call asks for a turn
(`needs=ollama:<model>`, app `llm_orchestration`) and releases it afterwards. Without a scheduler
(the default for new installs) model calls go straight to the model server.

## 7. Sites: watchdog, alerts, backups, failover

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

## 8. Clients

- Web UI (Vite/React) served by the daemon; Tauri app embeds `dist/` and talks to the hub with the token
  from `~/.config/llm-orchestration/node_token` (CSP set in `tauri.conf.json`).
- Long work (turns, agent runs, handoffs) are **jobs**: the request returns a `jobId`; progress streams over
  `/ws/events`; `GET /api/jobs/{id}` for state.

## 9. Files

| What | Where |
|---|---|
| Settings / node token | `~/.config/llm-orchestration/settings.json` (0600), `node_token` (0600) |
| DB, artifacts, runtime | `~/.local/share/llm-orchestration/` |
| TOTP secret | `<auth.totpStateDir>/totp_secret`, default `~/.local/share/llm-orchestration/totp_secret` (created by `scripts/new-totp.py`) |
| Vault | `~/LLM-Vault/` (both machines, synced) |
| Backups | `~/Backups/llm-orchestration/` (on the receiving machine; `paths.backupsDir`) |
| Units | `deploy/llm-orchestration.service`; tunnel and failover templates in `deploy/examples/` |
| Tunnel configs | e.g. `~/.cloudflared/orchestrator.yml` (primary), `failover-*.yml` (standby) |
