import { AlertTriangle, FolderGit2, Loader2, Lock, ServerCrash, ShieldAlert, ShieldCheck } from "lucide-react";
import { go, openPanel } from "@/state/actions";
import { setState, useApp } from "@/state/store";

function Pill({ label, tone, onClick, children, testId }: { label: string; tone: "ok" | "warn" | "bad" | "info"; onClick?: () => void; children: React.ReactNode; testId?: string }) {
  const color = tone === "ok" ? "var(--accent-emerald)" : tone === "warn" ? "var(--accent-amber)" : tone === "bad" ? "var(--accent-crimson)" : "var(--accent-primary)";
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label} data-testid={testId}
            className="pill inline-flex items-center gap-1 rounded-full border px-2 text-[0.7188rem] font-medium cursor-pointer shrink-0"
            style={{ borderColor: color, color, backgroundColor: "transparent" }}>
      {children}
    </button>
  );
}

/** Everything that might need you, at a glance. Each pill opens the place to deal with it. */
export function StatusStrip({ wide }: { wide: boolean }) {
  const status = useApp((s) => s.status);
  const connected = useApp((s) => s.connected);
  const approvals = useApp((s) => s.approvals.length);
  if (!status) return null;
  const open = (panel: "repos" | "sites") => (wide ? openPanel(panel) : go(panel));
  const running = status.jobsRunning.length;

  return (
    <div className="flex items-center gap-1.5 min-w-0 overflow-x-auto no-scrollbar" role="status" aria-label="System status">
      {status.nodes.map((n) => (
        <span key={n.id} className="pill inline-flex items-center gap-1 px-2 rounded-full text-[0.7188rem] shrink-0" style={{ backgroundColor: "var(--bg-elevated)" }}
              title={`${n.id}: ${n.online ? "online" : "offline"}${n.role === "hub" ? " (hub)" : ""}${n.self ? " · this machine" : ""}`}>
          <span className="w-2 h-2 rounded-full" style={{ backgroundColor: n.online ? "var(--accent-emerald)" : "var(--accent-crimson)" }} aria-hidden="true" />
          <span>{n.id}{n.self ? "" : n.online ? "" : " offline"}</span>
        </span>
      ))}
      {!connected && <Pill label="Live updates reconnecting" tone="warn"><Loader2 className="w-3 h-3 animate-spin" />{wide && "reconnecting"}</Pill>}
      {approvals > 0 && (
        <Pill label={`${approvals} command(s) waiting for your approval`} tone="warn" testId="pill-approvals">
          <ShieldAlert className="w-3 h-3" /> {approvals} to approve
        </Pill>
      )}
      {running > 0 && (
        <Pill label={`${running} job(s) running: ${status.jobsRunning.map((j) => j.title).join(", ")}`} tone="info">
          <Loader2 className="w-3 h-3 animate-spin" /> {running}{wide ? " running" : ""}
        </Pill>
      )}
      {(status.repoHoldsCount ?? 0) > 0 && (
        <Pill label={`${status.repoHoldsCount} repo(s) locked under active development`} tone="warn" onClick={() => setState({ locksOpen: true })} testId="pill-locks">
          <Lock className="w-3 h-3" /> {status.repoHoldsCount}{wide ? " locked" : ""}
        </Pill>
      )}
      {status.repoConflicts24h > 0 && (
        <Pill label={`${status.repoConflicts24h} repo handoff(s) need you`} tone="warn" onClick={() => open("repos")}>
          <FolderGit2 className="w-3 h-3" /> {status.repoConflicts24h}{wide ? " repo issue(s)" : ""}
        </Pill>
      )}
      {status.sitesDown.length > 0 ? (
        <Pill label={`Down: ${status.sitesDown.join(", ")}`} tone="bad" onClick={() => open("sites")} testId="pill-sites">
          <ServerCrash className="w-3 h-3" /> {status.sitesDown.length}{wide ? " site(s) down" : ""}
        </Pill>
      ) : wide ? (
        <Pill label="All sites healthy" tone="ok" onClick={() => open("sites")} testId="pill-sites"><ShieldCheck className="w-3 h-3" /> sites ok</Pill>
      ) : null}
      {Object.entries(status.agentsOut ?? {}).map(([id, a]) => (
        <Pill key={id} label={`${a.name} is out of usage until ${new Date(a.until * 1000).toLocaleString()}; work goes to the next agent`} tone="warn" testId={`pill-out-${id}`}>
          <AlertTriangle className="w-3 h-3" /> {a.name} out{wide ? ` until ${new Date(a.until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}
        </Pill>
      ))}
      {status.failover === "active" && <Pill label="This machine is standing in for the main one" tone="warn"><AlertTriangle className="w-3 h-3" /> failover</Pill>}
    </div>
  );
}
