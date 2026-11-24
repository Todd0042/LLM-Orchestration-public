import { useEffect, useMemo, useState } from "react";
import { Lock, RefreshCw, Unlock, X } from "lucide-react";
import { fetchRepoTracker, setRepoHold, type RepoTrackerRow } from "@/lib/api";
import { refreshStatus, toast } from "@/state/actions";

interface RepoLocksModalProps {
  open: boolean;
  onClose: () => void;
}

function fmtAge(ts: number): string {
  if (!ts) return "unknown";
  const sec = Math.max(0, Date.now() / 1000 - ts);
  if (sec < 90) return "just now";
  if (sec < 5400) return `${Math.round(sec / 60)}m ago`;
  if (sec < 129600) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

export function RepoLocksModal({ open, onClose }: RepoLocksModalProps) {
  const [rows, setRows] = useState<RepoTrackerRow[]>([]);
  const [filter, setFilter] = useState<"all" | "locked" | "attention">("all");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");

  const load = async (refresh = false) => {
    setBusy(refresh ? "*" : "init");
    try {
      const res = await fetchRepoTracker(refresh);
      setRows(res.repos);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    if (open) void load(false);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const lockedCount = useMemo(() => rows.filter((r) => r.hold?.state === "active").length, [rows]);
  const attentionCount = useMemo(
    () => rows.filter((r) => r.threeWayStatus !== "in_sync" || r.hold?.state === "needs_answer").length,
    [rows]
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (q && !r.repoName.toLowerCase().includes(q)) return false;
      if (filter === "locked") return r.hold?.state === "active";
      if (filter === "attention") return r.threeWayStatus !== "in_sync" || r.hold?.state === "needs_answer";
      return true;
    });
  }, [rows, filter, query]);

  if (!open) return null;

  const toggleHold = async (r: RepoTrackerRow) => {
    const active = r.hold?.state === "active";
    const nextState = active ? "clear" : "active";
    const note = active ? "" : r.activeProcess ? `active ${r.activeProcess}` : "locked in active development";
    setBusy(r.repoName);
    try {
      await setRepoHold(r.repoName, nextState, note);
      await Promise.all([load(false), refreshStatus()]);
      toast(`${r.repoName}: ${active ? "unlocked for sync" : "locked (skipped by sync & preflight)"}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-xs p-2 lg:p-4" onClick={onClose}>
      <div
        className="w-full max-w-[calc(100vw-16px)] max-h-[calc(100dvh-16px)] lg:max-w-4xl lg:h-[86dvh] flex flex-col rounded-xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-strong)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-2.5 border-b flex flex-wrap items-center justify-between gap-2 shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <div className="flex items-center gap-2 min-w-0">
            <Lock className="w-4 h-4 shrink-0" style={{ color: "var(--accent-amber)" }} />
            <h3 className="text-sm font-semibold truncate">Repo Locks &amp; 3-Way Tracker (Laptop · Desktop · GitHub)</h3>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              disabled={busy === "*"}
              onClick={() => void load(true)}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-xs cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
            >
              <RefreshCw className={`w-3.5 h-3.5 ${busy === "*" ? "animate-spin" : ""}`} />
              Rescan 3-Way
            </button>
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-xs cursor-pointer"
              style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
            >
              <X className="w-3.5 h-3.5" />
              Close
            </button>
          </div>
        </div>

        <div className="px-4 py-2 border-b flex flex-wrap items-center gap-2 shrink-0" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-muted)" }}>
          {(
            [
              ["all", `All (${rows.length})`],
              ["locked", `Locked (${lockedCount})`],
              ["attention", `Needs Attention (${attentionCount})`],
            ] as const
          ).map(([k, lbl]) => (
            <button
              key={k}
              type="button"
              onClick={() => setFilter(k)}
              className="px-2.5 py-1 rounded text-xs font-medium border cursor-pointer"
              style={{
                borderColor: filter === k ? "var(--accent-primary)" : "var(--border-subtle)",
                backgroundColor: filter === k ? "var(--accent-primary-soft)" : "var(--bg-elevated)",
                color: filter === k ? "var(--accent-primary)" : "var(--text-secondary)",
              }}
            >
              {lbl}
            </button>
          ))}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter repos… (sorted by last change)"
            className="ml-auto min-w-[12rem] rounded border px-2.5 py-1 text-xs outline-none"
            style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
          />
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-2">
          {visible.length === 0 ? (
            <p className="text-xs text-center py-8" style={{ color: "var(--text-muted)" }}>
              {busy ? "Scanning repositories…" : "No repositories match this filter."}
            </p>
          ) : (
            visible.map((r) => {
              const held = r.hold?.state === "active";
              const needsAnswer = r.hold?.state === "needs_answer";
              const diffSummary = r.desktop?.diffStat || r.laptop?.diffStat || "";
              return (
                <div
                  key={r.repoKey}
                  className="rounded-lg border p-2.5 text-xs space-y-1.5"
                  style={{
                    borderColor: held ? "var(--accent-amber)" : needsAnswer ? "var(--accent-crimson)" : "var(--border-subtle)",
                    backgroundColor: "var(--bg-elevated)",
                  }}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2 min-w-0">
                      <span className="font-semibold text-sm truncate">{r.repoName}</span>
                      <span className="text-[11px] font-mono-code" style={{ color: "var(--text-muted)" }} title={r.lastChangeAt ? new Date(r.lastChangeAt * 1000).toLocaleString() : ""}>
                        changed {fmtAge(r.lastChangeAt)}
                      </span>
                      {r.activeProcess && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold" style={{ backgroundColor: "var(--accent-amber)", color: "#0b0e14" }}>
                          {r.activeProcess} active
                        </span>
                      )}
                      <span
                        className="px-1.5 py-0.5 rounded border text-[10px] font-mono-code"
                        style={{
                          borderColor: r.threeWayStatus === "in_sync" ? "var(--accent-emerald)" : "var(--accent-amber)",
                          color: r.threeWayStatus === "in_sync" ? "var(--accent-emerald)" : "var(--accent-amber)",
                        }}
                      >
                        {r.threeWayStatus}
                      </span>
                    </div>
                    <button
                      type="button"
                      disabled={busy === r.repoName}
                      onClick={() => void toggleHold(r)}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-xs font-semibold cursor-pointer disabled:opacity-50"
                      style={{
                        borderColor: held ? "var(--accent-amber)" : "var(--border-strong)",
                        backgroundColor: held ? "var(--accent-amber)" : "var(--bg-panel)",
                        color: held ? "#0b0e14" : "var(--text-primary)",
                      }}
                    >
                      {held ? <Unlock className="w-3.5 h-3.5" /> : <Lock className="w-3.5 h-3.5" />}
                      {held ? "Unlock" : "Lock"}
                    </button>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-3 gap-1.5 font-mono-code text-[11px]" style={{ color: "var(--text-secondary)" }}>
                    <div>
                      <span style={{ color: "var(--text-muted)" }}>laptop: </span>
                      {r.laptop ? `${r.laptop.branch}@${r.laptop.headSha}${r.laptop.dirtyCount + r.laptop.untrackedCount > 0 ? ` (${r.laptop.dirtyCount + r.laptop.untrackedCount} dirty)` : ""}` : "not cloned"}
                    </div>
                    <div>
                      <span style={{ color: "var(--text-muted)" }}>desktop: </span>
                      {r.desktop ? `${r.desktop.branch}@${r.desktop.headSha}${r.desktop.dirtyCount + r.desktop.untrackedCount > 0 ? ` (${r.desktop.dirtyCount + r.desktop.untrackedCount} dirty)` : ""}` : "not cloned"}
                    </div>
                    <div>
                      <span style={{ color: "var(--text-muted)" }}>github: </span>
                      {r.github ? `${r.github.branch}@${r.github.headSha || "?"} (${r.github.branchCount} br)` : "no ls-remote"}
                    </div>
                  </div>

                  {diffSummary && (
                    <pre className="text-[10.5px] font-mono-code p-1.5 rounded border overflow-x-auto max-h-24" style={{ backgroundColor: "var(--bg-canvas)", borderColor: "var(--border-subtle)", color: "var(--text-muted)" }}>
                      {diffSummary}
                    </pre>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
