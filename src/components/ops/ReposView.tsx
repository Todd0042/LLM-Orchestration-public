import { useEffect, useMemo, useState } from "react";
import { ArrowRight, Lock, RefreshCw } from "lucide-react";
import { fetchRepoBoard, startHandoff, waitForJob, type HandoffRecord, type RepoBoardRow } from "@/lib/api";
import { refreshStatus, toast } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { IconButton } from "@/components/ui/IconButton";

const STATE: Record<RepoBoardRow["status"], { text: string; color: string }> = {
  in_sync: { text: "In sync", color: "var(--accent-emerald)" },
  dirty: { text: "Unfinished changes", color: "var(--accent-amber)" },
  branch_mismatch: { text: "Different branches", color: "var(--accent-amber)" },
  different_commits: { text: "Different commits", color: "var(--accent-amber)" },
  single_node: { text: "One machine only", color: "var(--text-muted)" },
};
const URGENCY: Record<string, number> = { review: 0, handoff: 1, gitignore: 2, none: 3 };

function ago(ts?: number): string {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}

/** Every repo on every machine, what to do about it, and one tap to do it. */
export function ReposView() {
  const [rows, setRows] = useState<RepoBoardRow[]>([]);
  const [handoffs, setHandoffs] = useState<HandoffRecord[]>([]);
  const [nodes, setNodes] = useState<string[]>([]);
  const [filter, setFilter] = useState<"attention" | "all">("attention");
  const [busy, setBusy] = useState("");
  const [loaded, setLoaded] = useState(false);
  const holdsCount = useApp((s) => s.status?.repoHoldsCount ?? 0);

  const load = async (refresh = false) => {
    setBusy(refresh ? "*" : "");
    try {
      const b = await fetchRepoBoard(refresh);
      setRows(b.repos);
      setHandoffs(b.handoffs);
      setNodes([b.nodeId, ...Object.keys(b.peersOnline)]);
      setLoaded(true);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    void load(false);
  }, []);

  const handoff = async (repo: string, to: string) => {
    setBusy(repo);
    try {
      const { jobId } = await startHandoff(repo, to);
      const job = await waitForJob(jobId);
      const r = job.result as { status?: string; message?: string; actions?: string[] };
      const ok = job.status === "done" && (r.status === "handed_off" || r.status === "already_in_sync");
      toast(ok ? `${repo}: ${r.actions?.join("; ") || "already in sync"}` : `${repo}: ${r.message || job.error || r.status}`, ok ? "info" : "error");
      await load(true);
      void refreshStatus();
    } finally {
      setBusy("");
    }
  };

  const shown = useMemo(() => {
    const list = filter === "all" ? rows : rows.filter((r) => r.recommendation?.action && r.recommendation.action !== "none");
    return [...list].sort((a, b) => (URGENCY[a.recommendation?.action] ?? 3) - (URGENCY[b.recommendation?.action] ?? 3) || a.repo.localeCompare(b.repo));
  }, [rows, filter]);
  const attention = rows.filter((r) => r.recommendation?.action && r.recommendation.action !== "none").length;

  return (
    <div className="p-3 sm:p-4 space-y-3" data-testid="sync-board">
      <div className="flex flex-wrap items-center gap-2">
        <IconButton label="Rescan all machines" showLabel onClick={() => void load(true)} disabled={busy === "*"}>
          <RefreshCw className={`w-4 h-4 ${busy === "*" ? "animate-spin" : ""}`} />
        </IconButton>
        <IconButton label={`Locks (${holdsCount})`} showLabel onClick={() => setState({ locksOpen: true })}>
          <Lock className="w-4 h-4" style={{ color: holdsCount > 0 ? "var(--accent-amber)" : undefined }} />
        </IconButton>
        <div role="tablist" aria-label="Which repos" className="inline-flex rounded-md border overflow-hidden" style={{ borderColor: "var(--border-subtle)" }}>
          {(["attention", "all"] as const).map((f) => (
            <button key={f} role="tab" aria-selected={filter === f} type="button" onClick={() => setFilter(f)} className="tap px-3 text-xs cursor-pointer"
                    style={{ backgroundColor: filter === f ? "var(--accent-primary-soft)" : "transparent", color: filter === f ? "var(--accent-primary)" : "var(--text-secondary)" }}>
              {f === "attention" ? `Needs attention (${attention})` : `All ${rows.length}`}
            </button>
          ))}
        </div>
      </div>

      {loaded && shown.length === 0 && (
        <p className="text-sm py-6 text-center" style={{ color: "var(--text-muted)" }}>{rows.length ? "Every repo is in sync. 🎉" : "No repos found."}</p>
      )}

      <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 21rem), 1fr))" }}>
        {shown.map((r) => {
          const rec = r.recommendation ?? { action: "none", text: "" };
          return (
            <article key={r.repo} data-repo={r.repo} className="rounded-xl border p-3 space-y-2 text-xs" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
              <header className="flex items-center gap-2">
                <h3 className="text-sm font-semibold truncate">{r.repo}</h3>
                <span className="ml-auto shrink-0 text-[0.6875rem]" style={{ color: STATE[r.status].color }}>{STATE[r.status].text}</span>
              </header>
              <dl className="space-y-0.5 font-mono-code text-[0.6875rem]">
                {nodes.map((n) => {
                  const st = r.nodes[n];
                  const changed = st ? st.dirtyCount + st.untrackedCount : 0;
                  return (
                    <div key={n} className="flex gap-2 min-w-0">
                      <dt className="w-16 shrink-0" style={{ color: "var(--text-muted)" }}>{n}{r.active?.nodeId === n ? " ●" : ""}</dt>
                      <dd className="truncate">
                        {st ? (
                          <>
                            {st.branch}@{st.headSha}
                            {changed > 0 && <span style={{ color: "var(--accent-amber)" }}> · {changed} changed</span>}
                            {st.ahead > 0 && <span> · {st.ahead} unpushed</span>}
                          </>
                        ) : (
                          <span style={{ color: "var(--text-muted)" }}>not cloned</span>
                        )}
                      </dd>
                    </div>
                  );
                })}
              </dl>
              {rec.text && <p style={{ color: rec.action === "none" ? "var(--text-muted)" : "var(--text-secondary)" }}>{rec.text}</p>}
              <div className="flex flex-wrap items-center gap-1.5">
                {rec.action === "handoff" && rec.from && rec.to && (
                  <button type="button" disabled={!!busy} onClick={() => void handoff(r.repo, rec.to!)} className="tap inline-flex items-center gap-1.5 px-3 rounded-md font-semibold cursor-pointer disabled:opacity-50"
                          style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>
                    {busy === r.repo ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <ArrowRight className="w-3.5 h-3.5" />}
                    Bring to {rec.to}
                  </button>
                )}
                {Object.keys(r.nodes).length > 1 &&
                  nodes.filter((n) => !(rec.action === "handoff" && rec.to === n)).map((n) => (
                    <button key={n} type="button" disabled={!!busy} onClick={() => void handoff(r.repo, n)} className="tap inline-flex items-center gap-1 px-2.5 rounded-md border cursor-pointer disabled:opacity-50"
                            style={{ borderColor: "var(--border-subtle)" }}>
                      {busy === r.repo ? <RefreshCw className="w-3 h-3 animate-spin" /> : <ArrowRight className="w-3 h-3" />} {n}
                    </button>
                  ))}
                {r.active && <span className="ml-auto text-[0.6562rem]" style={{ color: "var(--text-muted)" }}>working on {r.active.nodeId} · {ago(r.active.lastSeen)}</span>}
              </div>
            </article>
          );
        })}
      </div>

      {handoffs.length > 0 && (
        <section className="space-y-1 pt-2">
          <h3 className="text-xs font-semibold">Recent handoffs</h3>
          <ul className="space-y-1">
            {handoffs.slice(0, 12).map((h) => (
              <li key={h.id} className="text-[0.7188rem] flex flex-wrap gap-x-2">
                <span style={{ color: "var(--text-muted)" }}>{ago(h.createdAt)}</span>
                <span className="font-medium">{h.repo}</span>
                <span>{h.from} → {h.to}</span>
                <span style={{ color: h.status === "handed_off" ? "var(--text-secondary)" : "var(--accent-amber)" }}>{h.detail.message || h.status.replace(/_/g, " ")}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
