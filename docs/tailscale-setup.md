# Optional: connect the two machines with Tailscale

**You don't need this while both machines are on the same network.** Over the LAN, each machine reaches
the other at its local address (for example `192.0.2.10` and `192.0.2.20`), and that's all the sync needs.

Set this up if one machine travels, or if your network may change the machines' addresses. Tailscale is
a free private network for your own devices: each machine gets an address that works from anywhere, and
only your devices can use it.

## 1. Install and sign in, on both machines
```bash
# install tailscale with your package manager, then:
sudo systemctl enable --now tailscaled
sudo tailscale up          # prints a link: open it and log in (use the SAME account on both)
tailscale ip -4            # note this machine's address (100.x.y.z)
```

Check they can see each other (on either machine):
```bash
tailscale status           # both machines listed
curl http://<other machine's 100.x address>:8790/api/health
```

## 2. Tell LLM-Orchestration about the new addresses
Each machine keeps its LAN address first and adds the Tailscale one as a backup; it uses whichever
answers. Run in the repo folder on each machine:

Hub:
```bash
python3 scripts/configure_node.py --id desktop --role hub \
  --peer laptop=http://192.0.2.20:8790 --peer laptop=http://<laptop 100.x address>:8790
systemctl --user restart llm-orchestration
```

Worker:
```bash
python3 scripts/configure_node.py --id laptop --role worker \
  --peer desktop=http://192.0.2.10:8790 --peer desktop=http://<desktop 100.x address>:8790
systemctl --user restart llm-orchestration
```
(`configure_node.py` keeps everything else in the settings: services, backups, failover.)

Check: open Settings → peers, or run `curl -s http://127.0.0.1:8790/api/nodes` on either machine; the
other one should show `online: true`.

## Notes
- The machine-to-machine token still protects every request; Tailscale adds a private path, it doesn't
  replace that.
- Tailscale names ending in `.ts.net` are allowed by default, so you can use names instead of the 100.x
  addresses if you turn on MagicDNS in the Tailscale admin page.
- Failover is unaffected: it also checks the public address, so a Tailscale hiccup can't make the standby
  take over by mistake.
