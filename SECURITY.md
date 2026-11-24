# Security

LLM-Orchestration gives AI agents and a terminal access to your machines and repositories, and can be
reached from the internet through a tunnel. Its security model is described in
[ARCHITECTURE.md §3](ARCHITECTURE.md#3-security-model); this page is the short version, plus how to report
a problem.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead: the repository's
**Security** tab → **Report a vulnerability**. Include what you found, how to reproduce it, and what an
attacker could do with it. You'll get a reply as soon as possible; fixes are released before details are
made public.

## What protects what

| Who's asking | How they get in |
| :--- | :--- |
| A program on the same machine (`127.0.0.1`) | Trusted, but still origin/host/CSRF-checked, so a web page in your browser can't use it. Set `auth.requireAuthEverywhere` if other people log in to the same machine. |
| Another machine on your LAN | The machine-to-machine token (from `~/.config/llm-orchestration/node_token`) or a signed-in session. |
| Anyone through your tunnel (`127.0.0.1:8791`) | **Always** a signed-in session: a passkey, a pairing link, or the 6-digit authenticator code. The node token is refused there. |

- **Sign-in codes** are RFC 6238, single-use, with per-IP and global lockouts. Until you run
  `scripts/new-totp.py`, code sign-in is off.
- **Browsers can't be turned against it**: foreign origins get 403, unknown `Host` names 421 (DNS
  rebinding), and every state-changing request needs a custom header.
- **Agents** run AGY / Claude Code without permission prompts only if you turn that on
  (`LLM_ORCH_AGENT_SKIP_PERMISSIONS=1` or per engine in Settings). Shell commands a model picks need your
  approval unless they're a single read-only command.
- **sudo passwords** go from the UI to memory to an askpass socket; they are never logged, stored or
  broadcast.
- **Secrets stay out of the repo**: the node token, the authenticator secret, sessions and passkeys live
  in `~/.config/llm-orchestration` and `~/.local/share/llm-orchestration` with `0600` permissions.

## Running it safely

- Expose only the tunnel-only listener (`127.0.0.1:8791`) to the internet, never port `8790`.
- Keep `security.publicHosts` to the host names you actually use.
- Anyone who can sign in can run agents and commands as your user. Treat sign-in like SSH access.
