import { useMemo, useState } from "react";
import { Archive, MessageSquarePlus, Pin, PinOff, RefreshCw, Repeat, Search } from "lucide-react";
import type { ChatSummary } from "@/types/orch";
import { archiveChat, newMainChat, openMainChat, openSession, refreshChats, setPinned } from "@/state/actions";
import { useApp } from "@/state/store";
import { IconButton } from "@/components/ui/IconButton";
import { Dropdown } from "@/components/ui/Dropdown";

const SOURCE_LABEL: Record<ChatSummary["source"], string> = { agy: "AGY", claude: "Claude", orchestration: "Orchestrator" };

function ago(ms: string): string {
  const s = Math.max(0, (Date.now() - Number(ms)) / 1000);
  if (!Number(ms)) return "";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/**
 * Your conversations with the orchestrator (pin, rename in place, archive), then the AGY / Claude
 * sessions on every machine. `mode` decides which part is shown when space is short.
 */
export function ChatList({ mode = "both" }: { mode?: "both" | "conversations" | "sessions" }) {
  const chats = useApp((s) => s.chats);
  const mainId = useApp((s) => s.mainChat?.id);
  const sessionId = useApp((s) => s.session?.id);
  const [q, setQ] = useState("");
  const [agent, setAgent] = useState<"all" | "agy" | "claude">("all");
  const [machine, setMachine] = useState("all");

  const { convos, sessions, machines } = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const match = (c: ChatSummary) => !needle || `${c.title} ${c.preview ?? ""} ${c.workspace ?? ""}`.toLowerCase().includes(needle);
    const sorted = [...chats].sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0));
    return {
      convos: sorted.filter((c) => c.source === "orchestration" && match(c)).sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))),
      sessions: sorted.filter((c) => c.source !== "orchestration" && match(c) && (agent === "all" || c.source === agent) && (machine === "all" || c.nodeId === machine)),
      machines: [...new Set(chats.filter((c) => c.source !== "orchestration").map((c) => c.nodeId))],
    };
  }, [chats, q, agent, machine]);

  return (
    <nav className="h-full min-h-0 flex flex-col" aria-label="Conversations and sessions" style={{ backgroundColor: "var(--bg-panel)" }}>
      <div className="shrink-0 p-2.5 space-y-2 border-b" style={{ borderColor: "var(--border-subtle)" }}>
        <div className="flex items-center gap-2">
          <label className="flex-1 flex items-center gap-1.5 rounded-md border px-2" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-elevated)" }}>
            <Search className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--text-muted)" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search chats" aria-label="Search chats" className="w-full bg-transparent py-1.5 text-sm outline-none" />
          </label>
          <IconButton label="Refresh" onClick={() => void refreshChats()}><RefreshCw className="w-4 h-4" /></IconButton>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {mode !== "sessions" && (
          <section className="p-2 space-y-1" aria-label="Conversations">
            <div className="flex items-center justify-between px-1">
              <h3 className="text-[0.6875rem] font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>Conversations</h3>
              <div className="flex gap-1">
                <IconButton label="New relay (two agents talk it through)" onClick={() => void newMainChat("Agent relay", "relay")}>
                  <Repeat className="w-3.5 h-3.5" />
                </IconButton>
                <IconButton label="New conversation" showLabel onClick={() => void newMainChat()} className="text-[0.6875rem]">
                  <MessageSquarePlus className="w-3.5 h-3.5" />
                </IconButton>
              </div>
            </div>
            {convos.length === 0 && <p className="px-1 text-xs" style={{ color: "var(--text-muted)" }}>No conversations yet.</p>}
            {convos.map((c) => (
              <div key={c.id} className="group flex items-center gap-1 rounded-lg" style={{ backgroundColor: c.id === mainId ? "var(--accent-primary-soft)" : undefined }}>
                <button type="button" onClick={() => void openMainChat(c)} className="flex-1 min-w-0 text-left px-2 py-2 cursor-pointer" aria-current={c.id === mainId ? "page" : undefined}>
                  <div className="flex items-center gap-1.5 text-sm">
                    {c.pinned && <Pin className="w-3 h-3 shrink-0" style={{ color: "var(--accent-primary)" }} />}
                    <span className="truncate font-medium">{c.title}</span>
                    <span className="ml-auto text-[0.6562rem] shrink-0" style={{ color: "var(--text-muted)" }}>{ago(c.updatedAt)}</span>
                  </div>
                  {c.preview && <div className="text-[0.7188rem] truncate" style={{ color: "var(--text-muted)" }}>{c.preview}</div>}
                </button>
                <div className="flex gap-1 pr-1 opacity-100 lg:opacity-0 lg:group-hover:opacity-100 lg:group-focus-within:opacity-100">
                  <IconButton label={c.pinned ? "Unpin" : "Pin"} onClick={() => void setPinned(c.id, !c.pinned)}>
                    {c.pinned ? <PinOff className="w-3.5 h-3.5" /> : <Pin className="w-3.5 h-3.5" />}
                  </IconButton>
                  <IconButton label="Archive" onClick={() => void archiveChat(c.id)}><Archive className="w-3.5 h-3.5" /></IconButton>
                </div>
              </div>
            ))}
          </section>
        )}
        {mode !== "conversations" && (
          <section className="p-2 space-y-1 border-t" style={{ borderColor: "var(--border-subtle)" }} aria-label="Agent sessions">
            <h3 className="px-1 text-[0.6875rem] font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>AGY &amp; Claude sessions</h3>
            <div className="flex flex-wrap items-center gap-1.5 px-1 pb-1">
              {(["all", "agy", "claude"] as const).map((a) => (
                <button key={a} type="button" onClick={() => setAgent(a)} aria-pressed={agent === a} className="px-2 py-0.5 rounded-full border text-[0.6875rem] cursor-pointer"
                        style={{ borderColor: agent === a ? "var(--accent-primary)" : "var(--border-subtle)", color: agent === a ? "var(--accent-primary)" : "var(--text-secondary)" }}>
                  {a === "all" ? "All" : SOURCE_LABEL[a]}
                </button>
              ))}
              {machines.length > 1 && (
                <Dropdown value={machine} onChange={setMachine} title="Machine" menuWidthClass="min-w-[9rem]"
                          options={[{ value: "all", label: "All machines" }, ...machines.map((m) => ({ value: m, label: m }))]} />
              )}
            </div>
            {sessions.length === 0 && <p className="px-1 text-xs" style={{ color: "var(--text-muted)" }}>No sessions match.</p>}
            {sessions.map((c) => (
              <button key={`${c.nodeId}:${c.id}`} type="button" onClick={() => void openSession(c)} aria-current={c.id === sessionId ? "page" : undefined}
                      className="w-full text-left rounded-lg px-2 py-2 cursor-pointer" style={{ backgroundColor: c.id === sessionId ? "var(--accent-primary-soft)" : undefined }}>
                <div className="flex items-center gap-1.5 text-sm">
                  <span className="text-[0.625rem] font-semibold px-1.5 rounded shrink-0" style={{ backgroundColor: "var(--bg-elevated)", color: c.source === "claude" ? "var(--accent-amber)" : "var(--accent-primary)" }}>
                    {SOURCE_LABEL[c.source]}
                  </span>
                  <span className="truncate">{c.title}</span>
                  <span className="ml-auto text-[0.6562rem] shrink-0" style={{ color: "var(--text-muted)" }}>{c.nodeId} · {ago(c.updatedAt)}</span>
                </div>
              </button>
            ))}
          </section>
        )}
      </div>
    </nav>
  );
}
