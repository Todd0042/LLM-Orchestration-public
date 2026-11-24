# Deploy examples

Templates for running LLM-Orchestration as a service, publishing it through a tunnel, and failing over to
a second machine. Copy what you need, rename to taste, and point your settings at it.

| File | Use |
| :--- | :--- |
| `../llm-orchestration.service` | The daemon as a systemd user unit (`@REPO_DIR@` is replaced with the repo path on install). |
| `orchestrator-tunnel.service` | Cloudflare Tunnel to the tunnel-only listener (`127.0.0.1:8791`, code sign-in always required). |
| `llm-orchestration-failover.target` | What a standby machine starts while the primary is down (`LLM_ORCH_FAILOVER_UNIT`). |
| `failover-*-tunnel.service` | Second tunnel connectors the failover target starts. |
| `services.example.json` | The service list the watchdog checks, restarts, backs up and fails over. Load it with `scripts/configure_node.py --services`. |

### `services.example.json` fields

| Field | Meaning |
| :--- | :--- |
| `id`, `name` | Identifier and display name. |
| `node` | Which machine runs it. |
| `publicHost` | The public host name (shown in the UI; used for failover pages). |
| `units` | systemd user units to restart when the health check fails. |
| `restartOrder` | Blue/green: restart these one at a time, waiting for health in between. |
| `container`, `containerFrom`, `compose` | Docker: the container to check, how to find the live one, and the Compose project to bring back up. |
| `health.url`, `health.okCodes` | Health check URL and the HTTP codes that count as healthy (default 2xx). |
| `backup.sqlite` | SQLite databases to snapshot and send to the other machine. |
| `failover.mode` | `maintenance`: the standby shows a "back soon" page for this site. |
