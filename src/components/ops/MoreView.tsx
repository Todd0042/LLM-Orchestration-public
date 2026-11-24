import { useState, type ReactNode } from "react";
import { Activity, ArrowLeft, ChevronRight, Fingerprint, FolderGit2, HardDrive, LogOut, Settings, Type, Wrench } from "lucide-react";
import { logoutTotp } from "@/lib/api";
import { openArtifact, refreshChats, setTextScale } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { OpsModal } from "@/components/OpsModal";
import { ActivityTab } from "./ActivityTab";
import { DevicesView } from "./DevicesView";
import { usePlugins } from "@/plugins";

/** A built-in screen, or the id of a plugin (src/plugins). */
type Sub = null | "devices" | "vault" | "repos" | "tools" | "activity" | (string & {});

const ITEMS: Array<{ id: string; label: string; hint: string; icon: ReactNode }> = [
  { id: "devices", label: "Devices & sign-in", hint: "Passkeys, pair your phone, signed-in devices", icon: <Fingerprint className="w-4 h-4" /> },
  { id: "vault", label: "Vault", hint: "Shared files, synced on both machines", icon: <HardDrive className="w-4 h-4" /> },
  { id: "activity", label: "Activity log", hint: "Logins, commands, handoffs, restarts", icon: <Activity className="w-4 h-4" /> },
  { id: "repos", label: "Catch-up queue", hint: "Work waiting for a machine to come back", icon: <FolderGit2 className="w-4 h-4" /> },
  { id: "tools", label: "Models & tools per machine", hint: "What each machine can run", icon: <Wrench className="w-4 h-4" /> },
];
const SCALES = [0.9, 1, 1.15, 1.3];

export function MoreView() {
  const [sub, setSub] = useState<Sub>(null);
  const plugins = usePlugins();
  const items = [...ITEMS, ...plugins.map((p) => ({ id: p.id, label: p.label, hint: p.hint, icon: <p.icon className="w-4 h-4" /> }))];
  const scale = useApp((s) => s.textScale);
  const auth = useApp((s) => s.auth);
  const anyOnline = useApp((s) => s.peersStatus.some((p) => p.online));
  const mainId = useApp((s) => s.mainChat?.id ?? "");

  if (sub) {
    const item = items.find((i) => i.id === sub);
    if (!item) return null;
    return (
      <div className="h-full min-h-0 flex flex-col">
        <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: "var(--border-subtle)" }}>
          <button type="button" onClick={() => setSub(null)} aria-label="Back" className="tap inline-flex items-center justify-center rounded-md border cursor-pointer" style={{ borderColor: "var(--border-subtle)" }}>
            <ArrowLeft className="w-4 h-4" />
          </button>
          <h3 className="text-sm font-semibold">{item.label}</h3>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto">
          {sub === "activity" ? (
            <div className="p-3"><ActivityTab /></div>
          ) : sub === "devices" ? (
            <DevicesView />
          ) : (
            <OpsModal open embedded initialTab={sub} desktopOnline={anyOnline} activeChatId={mainId} onOpenArtifact={openArtifact}
                      onRefreshChats={() => void refreshChats()} onClose={() => setSub(null)} />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="p-3 sm:p-4 space-y-4 max-w-2xl">
      <ul className="rounded-xl border divide-y overflow-hidden" style={{ borderColor: "var(--border-subtle)" }}>
        {items.map((i) => (
          <li key={i.id} style={{ borderColor: "var(--border-subtle)" }}>
            <button type="button" onClick={() => setSub(i.id)} className="w-full flex items-center gap-3 px-3 py-3 text-left cursor-pointer" style={{ backgroundColor: "var(--bg-panel)" }}>
              <span style={{ color: "var(--accent-primary)" }}>{i.icon}</span>
              <span className="flex-1 min-w-0">
                <span className="block text-sm font-medium">{i.label}</span>
                <span className="block text-xs truncate" style={{ color: "var(--text-muted)" }}>{i.hint}</span>
              </span>
              <ChevronRight className="w-4 h-4" style={{ color: "var(--text-muted)" }} />
            </button>
          </li>
        ))}
      </ul>

      <section className="rounded-xl border p-3 space-y-2" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Type className="w-4 h-4" /> Text size</h3>
        <div role="radiogroup" aria-label="Text size" className="flex gap-1.5">
          {SCALES.map((s) => (
            <button key={s} role="radio" aria-checked={scale === s} type="button" onClick={() => setTextScale(s)} className="tap flex-1 rounded-md border cursor-pointer"
                    style={{ borderColor: scale === s ? "var(--accent-primary)" : "var(--border-subtle)", color: scale === s ? "var(--accent-primary)" : "var(--text-secondary)", fontSize: `${s * 0.85}rem` }}>
              A
            </button>
          ))}
        </div>
      </section>

      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => setState({ settingsOpen: true })} className="tap inline-flex items-center gap-2 px-3 rounded-md border cursor-pointer" style={{ borderColor: "var(--border-subtle)" }}>
          <Settings className="w-4 h-4" /> Settings
        </button>
        {auth?.authRequired && (
          <button type="button" onClick={async () => { await logoutTotp(); window.location.reload(); }} className="tap inline-flex items-center gap-2 px-3 rounded-md border cursor-pointer" style={{ borderColor: "var(--border-subtle)" }}>
            <LogOut className="w-4 h-4" /> Sign out
          </button>
        )}
      </div>
    </div>
  );
}
