<p align="center">
  <a href="https://ko-fi.com/todd0042">
    <img src="https://raw.githubusercontent.com/Todd0042/creator-assets/main/banners/todd_creator_banner_v1.0.png" alt="Support Todd on Ko-fi" width="100%" />
  </a>
</p>

---

# 🧠 LLM-Orchestration

[![Version: 0.1.0](https://img.shields.io/badge/Version-0.1.0-ff2a85.svg)]()
[![Server: Python + FastAPI](https://img.shields.io/badge/Server-Python%20%2B%20FastAPI-3776AB.svg)]()
[![UI: React + Vite](https://img.shields.io/badge/UI-React%20%2B%20Vite-61DAFB.svg)]()
[![App: Tauri 2](https://img.shields.io/badge/App-Tauri%202-FFC131.svg)]()
[![Machines: 1 or 2](https://img.shields.io/badge/Machines-1%20or%202-teal.svg)]()
[![AI: Local first](https://img.shields.io/badge/AI-Local%20first-black.svg)]()
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-00f0ff.svg)](LICENSE)
[![Support on Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20Creator-F16061?style=flat&logo=ko-fi&logoColor=white)](https://ko-fi.com/todd0042)

> **One orchestrator you always talk to, across your machines: it routes work to local models, Antigravity (AGY) and Claude Code, keeps your repos and conversations in sync between two machines, watches and backs up your self-hosted sites, and fails over to the second machine when the first goes down.**

---

## 📖 Table of Contents

* **Part 1 — Overview & Quick Start**
  * [🧠 What Is LLM-Orchestration?](#-what-is-llm-orchestration)
  * [✨ Highlights at a Glance](#-highlights-at-a-glance)
  * [🚀 Quick Download & Installation](#-quick-download--installation)
* **Part 2 — Technical Documentation & Architecture Reference**
  * [1. Architecture & Operations ([ARCHITECTURE.md](ARCHITECTURE.md))](#1-architecture--operations)
  * [2. Server Modules ([server/app.py](server/app.py), [server/orchestrator.py](server/orchestrator.py), [server/repo_sync.py](server/repo_sync.py))](#2-server-modules)
  * [3. Configuration ([llm-orchestration.env.example](llm-orchestration.env.example), [server/config.py](server/config.py))](#3-configuration)
  * [4. Repository Structure](#4-repository-structure)
  * [5. Testing ([e2e/](e2e/), [server/test_orchestration.py](server/test_orchestration.py))](#5-testing)
  * [6. Ethics, Provenance & Licensing ([LICENSE](LICENSE))](#6-ethics-provenance--licensing)

---

# Part 1 — Overview & Quick Start

## 🧠 What Is LLM-Orchestration?

A small daemon that runs on each of your machines, plus a web UI (also installable on your phone) and a Tauri desktop app. One machine is the **hub** you talk to; a second, optional machine is a **worker** and **standby**:

1. **🗣️ One orchestrator**: a single conversation that hands work to a local model, AGY or Claude Code, on whichever machine fits, and streams the results back.
2. **🔁 Repo sync & handoff**: knows which machine is active for each repo and moves work between them, uncommitted changes included, without ever merging, resetting or force-pushing your work.
3. **🛰️ Sites**: a watchdog for your self-hosted services, phone alerts, SQLite backups to the other machine, and automatic failover.

It works on a single machine too: replication, backups and failover simply stay idle until you add a peer.

---

## ✨ Highlights at a Glance

* **🔐 Hardened remote access**: a 6-digit authenticator code or a passkey for people; origin and host allowlists, CSRF headers and per-zone rules (tunnel, LAN, local); a machine-to-machine token that never reaches a browser.
* **✅ Approvals**: shell commands a model chooses need your Approve click unless they're a single read-only command.
* **💬 One conversation on two machines**: chats, artifacts and a shared vault folder replicate both ways, with conflict copies instead of lost edits.
* **🧩 Optional plugins**: server and UI plugins are discovered at start-up and only shown when installed.
* **📱 Phone-friendly**: installable web app, thumb-sized controls, passkey sign-in and pairing links.

---

## 🚀 Quick Download & Installation

### 🐧 Linux (systemd user service)
Needs Python 3.11+, Node.js 20+ and git. Optional: `qrencode` (shows the sign-in QR code), a model server (llama.cpp server, Ollama or anything OpenAI-compatible), the `agy` and `claude` CLIs.
```bash
git clone https://github.com/Todd0042/LLM-Orchestration-public.git llm-orchestration
cd llm-orchestration
cp llm-orchestration.env.example llm-orchestration.env   # edit: repos folder, model server, public host…
scripts/install.sh            # venv, UI build, systemd user service, sign-in code (QR)
```
Open `http://127.0.0.1:8790`. From another device, sign in with the 6-digit code from your authenticator app, then add a passkey.

### 🖥️ Desktop app (optional)
```bash
scripts/install-launcher.sh   # builds the Tauri app and adds it to your applications menu
```

### 🔗 A second machine (optional)
Install it the same way, then link the two (see [scripts/configure_node.py](scripts/configure_node.py) and [docs/tailscale-setup.md](docs/tailscale-setup.md)):
```bash
scripts/configure_node.py --id desktop --role hub    --peer laptop=http://192.0.2.20:8790 --backups-to laptop
scripts/configure_node.py --id laptop  --role worker --peer desktop=http://192.0.2.10:8790 --failover-standby desktop
```

---

# Part 2 — Technical Documentation & Architecture Reference

> *The sections below are a code-verified engineering reference for this repository as it stands.*

---

## 1. Architecture & Operations

The full design and operations guide is **[ARCHITECTURE.md](ARCHITECTURE.md)**: process layout and listeners, the security model, repo sync and handoff, replication, GPU sharing, the site watchdog, backups and failover, clients and file locations.

---

## 2. Server Modules

| Module | Responsibility |
| :--- | :--- |
| `app.py` | App factory, security gate, `/ws/events`, the three listeners |
| `context.py` | Owns every service and background loop |
| `security.py` | Zones, allowlists, file-read roots, signed media URLs, WebSocket tickets |
| `auth.py` | Authenticator codes (RFC 6238, replay-protected) and brute-force lockouts |
| `identity.py` | Passkeys, pairing links, sessions and trusted devices |
| `core.py` | Broadcaster, audit log, approvals, jobs |
| `nodes.py` | Peers, health, capability profile |
| `llm.py` | Model routing, completions, optional GPU turns |
| `agents.py`, `cli_agents.py`, `agent_chain.py` | AGY / Claude Code dispatch, local or on the peer |
| `orchestrator.py`, `orch_tools.py` | The orchestrator tool loop |
| `repo_ops.py`, `repo_sync.py` | Non-destructive git primitives and the handoff engine |
| `replication.py` | Two-way replication of chats, repo activity and the vault |
| `monitor.py` | Watchdog, alerts, backups, failover |
| `plugins/` | Optional plugins (`build(ctx)` + `FEATURE`), listed at `GET /api/features` |
| `routes/` | HTTP routes per area |

---

## 3. Configuration

Settings live in `~/.config/llm-orchestration/settings.json` and are edited in the app (Settings) or with `scripts/configure_node.py`. On first start they're seeded from **`llm-orchestration.env`** (copy [llm-orchestration.env.example](llm-orchestration.env.example)); environment variables win over the file.

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `LLM_ORCH_NODE_ID`, `LLM_ORCH_ROLE` | detected | Machine id and role (`hub` / `worker`) |
| `LLM_ORCH_PORT` | `8790` | Main listener |
| `LLM_ORCH_GITHUB_DIR` | `~/GitHub` | Folder with your repositories |
| `LLM_ORCH_PUBLIC_HOSTS` | *(none)* | Host names your tunnel serves the UI on |
| `LLM_ORCH_TOTP_DIR` | `~/.local/share/llm-orchestration` | Where the sign-in secret is kept |
| `LLM_ORCH_LLM_BASE` | `http://127.0.0.1:8080/v1` | OpenAI-compatible model server |
| `LLM_ORCH_AGENT_SKIP_PERMISSIONS` | `0` | Let AGY / Claude Code run without permission prompts |
| `LLM_ORCH_NTFY_URL` | *(none)* | Phone alerts through ntfy |
| `LLM_ORCH_BACKUPS_DIR` | `~/Backups/llm-orchestration` | Where the other machine's backups land |
| `LLM_ORCH_FAILOVER_UNIT` | *(none)* | systemd unit the standby starts during failover |

Deploy templates (tunnel, failover, service list) are in [deploy/examples/](deploy/examples/README.md).

---

## 4. Repository Structure

```text
llm-orchestration/
├── server/                     # Python daemon (FastAPI)
│   ├── routes/                 # HTTP routes per area
│   └── plugins/                # Optional plugins
├── src/                        # Web UI (React + Vite + Tailwind)
│   └── plugins/                # Optional UI plugins
├── src-tauri/                  # Desktop app (Tauri 2)
├── scripts/                    # install.sh, new-totp.py, configure_node.py, setup-venv.sh, …
├── deploy/                     # systemd unit and examples/ (tunnel, failover, services)
├── docs/                       # Tailscale guide
├── e2e/                        # Two-node end-to-end and Playwright UI tests
├── tools/                      # Extra orchestrator tools (Python, loaded at start-up)
├── llm-orchestration.env.example
├── ARCHITECTURE.md
└── SECURITY.md
```

---

## 5. Testing

```bash
npm run test:e2e     # two real nodes, fake models/CLIs, real git repos (security, sync, failover…)
npm run test:ui      # Playwright against the built UI on a two-node cluster
python3 -m unittest server.test_orchestration
```

---

## 6. Ethics, Provenance & Licensing

### 🔒 Privacy
Everything runs on your own machines. Nothing is sent anywhere except to the model servers and CLIs you configure, the peer machine, and (if you set it up) your ntfy topic. See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

### ⚖️ Software License & Commercial Inquiries
* **Non-Commercial Use (Free, Source-Available)**: LLM-Orchestration is licensed for personal, hobbyist, educational, and non-commercial use under the **[PolyForm Noncommercial License 1.0.0](LICENSE)**.
* **Commercial Licensing Inquiries**: commercial use (inside a company, in a paid product or service, or for profit) requires a separate license from [Todd0042](https://github.com/Todd0042).
* **Media**: the app icons in `public/icons/` and `src-tauri/icons/` are © Todd0042 under [CC BY-NC 4.0](LICENSE-MEDIA).
* **Third-Party Components**: AGY, Claude Code, Ollama, llama.cpp, ComfyUI, Cloudflare Tunnel and Tailscale are separate products with their own terms.

---

<p align="center">
  If you enjoy LLM-Orchestration, consider supporting its development:<br/><br/>
  <a href="https://ko-fi.com/todd0042" target="_blank">
    <img src="https://img.shields.io/badge/☕_Support_LLM--Orchestration_on_Ko--fi-F16061?style=for-the-badge&logo=ko-fi&logoColor=white" alt="Support on Ko-fi">
  </a>
</p>

---

<p align="center">
  <a href="https://ko-fi.com/todd0042">
    <img src="https://raw.githubusercontent.com/Todd0042/creator-assets/main/banners/todd_creator_banner_v2.0.png" alt="Support Todd on Ko-fi" width="100%" />
  </a>
</p>
