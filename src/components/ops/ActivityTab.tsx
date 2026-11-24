import { useEffect, useState } from "react";
import { fetchAudit, type AuditEntry } from "@/lib/api";

/** Append-only record: who asked for what (and from where), and which machine ran it. */
export function ActivityTab() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [q, setQ] = useState("");

  useEffect(() => {
    void fetchAudit(500).then(setRows).catch(() => setRows([]));
  }, []);

  const shown = q ? rows.filter((r) => `${r.action} ${r.target} ${r.zone}`.toLowerCase().includes(q.toLowerCase())) : rows;

  return (
    <div className="space-y-2" data-testid="activity-tab">
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter (e.g. terminal, login, handoff, tunnel)" className="w-full px-2 py-1 rounded-md border text-xs bg-transparent" style={{ borderColor: "var(--border-subtle)" }} />
      <div className="space-y-0.5">
        {shown.map((r) => (
          <div key={`${r.nodeId}-${r.id}`} className="text-[0.6875rem] flex gap-2">
            <span style={{ color: "var(--text-muted)" }}>{new Date(r.ts * 1000).toLocaleString()}</span>
            <span>{r.nodeId}</span>
            <span style={{ color: r.zone === "tunnel" ? "var(--accent-amber)" : "var(--text-secondary)" }}>{r.zone}</span>
            <span className="font-medium">{r.action}</span>
            <span className="font-mono-code break-all">{r.target}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
