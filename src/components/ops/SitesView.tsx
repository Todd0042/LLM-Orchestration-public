import { useEffect, useState } from "react";
import { DatabaseBackup, RefreshCw, ShieldCheck } from "lucide-react";
import { fetchServiceEvents, fetchServices, runBackupsNow, type ServiceStatus } from "@/lib/api";
import { toast } from "@/state/actions";
import { IconButton } from "@/components/ui/IconButton";

const LOOK: Record<ServiceStatus["status"], { text: string; color: string }> = {
  healthy: { text: "Healthy", color: "var(--accent-emerald)" },
  failing: { text: "Failing", color: "var(--accent-amber)" },
  restarting: { text: "Restarting", color: "var(--accent-amber)" },
  gave_up: { text: "Down: stopped retrying", color: "var(--accent-crimson)" },
  unknown: { text: "Checking…", color: "var(--text-muted)" },
};

/** Your sites and services on each machine, backups, and the standby machine. */
export function SitesView() {
  const [data, setData] = useState<Awaited<ReturnType<typeof fetchServices>> | null>(null);
  const [events, setEvents] = useState<Awaited<ReturnType<typeof fetchServiceEvents>>>([]);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [d, e] = await Promise.all([fetchServices(), fetchServiceEvents()]);
      setData(d);
      setEvents(e);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    }
  };

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(t);
  }, []);

  const fo = data?.failover;
  const lastBackup = data?.backups?.at;

  return (
    <div className="p-3 sm:p-4 space-y-4" data-testid="services-tab">
      <div className="flex flex-wrap items-center gap-2">
        <IconButton label="Refresh" showLabel onClick={() => void load()}><RefreshCw className="w-4 h-4" /></IconButton>
        <IconButton label="Back up databases now" showLabel disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            const r = await runBackupsNow();
            const res = (r.results ?? {}) as Record<string, string>;
            const bad = Object.entries(res).filter(([, v]) => v !== "sent" && v !== "stored locally");
            toast(bad.length ? `Backups: ${bad.length} problem(s): ${bad.map(([k, v]) => `${k} ${v}`).join("; ")}` : `Backed up ${Object.keys(res).length} databases`, bad.length ? "error" : "info");
            await load();
          } finally {
            setBusy(false);
          }
        }}>
          <DatabaseBackup className="w-4 h-4" />
        </IconButton>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="rounded-xl border p-3 text-xs space-y-1" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
          <div className="flex items-center gap-2 font-semibold text-sm"><ShieldCheck className="w-4 h-4" style={{ color: fo?.state === "active" ? "var(--accent-amber)" : "var(--accent-emerald)" }} /> Backup machine</div>
          <p style={{ color: "var(--text-secondary)" }}>
            {!fo?.enabled
              ? "No standby machine is set up."
              : fo.state === "active"
                ? `${fo.standbyNode} is serving your sites right now because ${fo.primaryNode} is unreachable.`
                : `${fo.standbyNode} is on standby. If ${fo.primaryNode} is down for 2 minutes, it takes over your sites.`}
          </p>
        </div>
        <div className="rounded-xl border p-3 text-xs space-y-1" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
          <div className="flex items-center gap-2 font-semibold text-sm"><DatabaseBackup className="w-4 h-4" /> Database backups</div>
          <p style={{ color: "var(--text-secondary)" }}>
            {lastBackup ? `Last run ${new Date(lastBackup * 1000).toLocaleString()} → ${data?.backups?.target || "this machine"}. Every 6 hours, keeping 14.` : "No backup has run yet since the last restart."}
          </p>
        </div>
      </div>

      {data &&
        Object.entries(data.nodes).map(([node, list]) => (
          <section key={node} className="space-y-1.5" aria-label={`Services on ${node}`}>
            <h3 className="text-sm font-semibold">{node}</h3>
            {list === null ? (
              <p className="text-xs" style={{ color: "var(--accent-amber)" }}>Offline: can't check this machine right now.</p>
            ) : list.length === 0 ? (
              <p className="text-xs" style={{ color: "var(--text-muted)" }}>Nothing watched on this machine.</p>
            ) : (
              <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 16rem), 1fr))" }}>
                {[...list].sort((a, b) => Number(a.status === "healthy") - Number(b.status === "healthy")).map((s) => (
                  <div key={s.id} data-service={s.id} className="rounded-xl border px-3 py-2 text-xs" style={{ borderColor: s.status === "healthy" ? "var(--border-subtle)" : LOOK[s.status].color, backgroundColor: "var(--bg-panel)" }}>
                    <div className="flex items-center gap-2">
                      <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: LOOK[s.status].color }} aria-hidden="true" />
                      <span className="font-medium text-sm truncate">{s.name}</span>
                      <span className="ml-auto shrink-0 text-[0.6875rem]" style={{ color: LOOK[s.status].color }}>{LOOK[s.status].text}</span>
                    </div>
                    {s.publicHost && (
                      <a href={`https://${s.publicHost}`} target="_blank" rel="noreferrer noopener" className="block truncate text-[0.6875rem] mt-0.5" style={{ color: "var(--text-muted)" }}>
                        {s.publicHost}
                      </a>
                    )}
                    {s.status !== "healthy" && s.detail && <p className="mt-1 break-all" style={{ color: LOOK[s.status].color }}>{s.detail}</p>}
                    {s.restartsLastHour > 0 && <p className="mt-0.5" style={{ color: "var(--text-muted)" }}>{s.restartsLastHour} restart(s) this hour</p>}
                  </div>
                ))}
              </div>
            )}
          </section>
        ))}

      {events.length > 0 && (
        <section className="space-y-1" aria-label="Watchdog history">
          <h3 className="text-sm font-semibold">What the watchdog did</h3>
          <ul className="space-y-0.5">
            {events.slice(0, 40).map((e) => (
              <li key={e.id} className="text-[0.7188rem] flex flex-wrap gap-x-2">
                <span style={{ color: "var(--text-muted)" }}>{new Date(e.ts * 1000).toLocaleString()}</span>
                <span>{e.node_id}</span>
                <span className="font-medium">{e.service_id}</span>
                <span>{e.kind.replace(/_/g, " ")}</span>
                <span className="break-all" style={{ color: "var(--text-secondary)" }}>{e.detail}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
