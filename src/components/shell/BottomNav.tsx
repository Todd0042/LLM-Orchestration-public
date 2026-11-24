import { Bot, FolderGit2, LayoutGrid, MessageSquare, Server } from "lucide-react";
import { go } from "@/state/actions";
import { useApp, type View } from "@/state/store";

const TABS: Array<{ id: View; label: string; icon: typeof Bot }> = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "sessions", label: "Agents", icon: Bot },
  { id: "repos", label: "Repos", icon: FolderGit2 },
  { id: "sites", label: "Sites", icon: Server },
  { id: "more", label: "More", icon: LayoutGrid },
];

/** Phone/tablet navigation: thumb-reachable, with badges for things that need you. */
export function BottomNav() {
  const view = useApp((s) => s.view);
  const sitesDown = useApp((s) => s.status?.sitesDown.length ?? 0);
  const repoIssues = useApp((s) => s.status?.repoConflicts24h ?? 0);
  const badge: Partial<Record<View, number>> = { sites: sitesDown, repos: repoIssues };

  return (
    <nav className="shrink-0 grid grid-cols-5 border-t" aria-label="Main" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)", paddingBottom: "env(safe-area-inset-bottom)" }}>
      {TABS.map(({ id, label, icon: Icon }) => {
        const on = view === id;
        return (
          <button key={id} type="button" onClick={() => go(id)} aria-current={on ? "page" : undefined}
                  className="relative flex flex-col items-center justify-center gap-0.5 h-14 text-[0.6875rem] cursor-pointer"
                  style={{ color: on ? "var(--accent-primary)" : "var(--text-secondary)" }}>
            <Icon className="w-5 h-5" />
            {label}
            {!!badge[id] && (
              <span className="absolute top-1.5 right-[calc(50%-18px)] min-w-4 h-4 px-1 rounded-full text-[0.625rem] font-bold leading-4 text-center"
                    style={{ backgroundColor: id === "sites" ? "var(--accent-crimson)" : "var(--accent-amber)", color: "#0b0e14" }}>
                {badge[id]}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
