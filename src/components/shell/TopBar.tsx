import { FolderGit2, LayoutGrid, Lock, Server, Settings } from "lucide-react";
import { openPanel } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { IconButton } from "@/components/ui/IconButton";
import { StatusStrip } from "./StatusStrip";

/** One row: name, status, and (on wide screens) the places you go besides the chat. */
export function TopBar({ wide }: { wide: boolean }) {
  const holdsCount = useApp((s) => s.status?.repoHoldsCount ?? 0);
  return (
    <header className="shrink-0 flex items-center gap-2 px-3 h-12 border-b" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)", paddingTop: "env(safe-area-inset-top)" }}>
      <span className="font-bold text-sm shrink-0" style={{ color: "var(--accent-primary)" }}>LLM-Orch</span>
      <div className="flex-1 min-w-0"><StatusStrip wide={wide} /></div>
      {wide && (
        <nav className="flex items-center gap-1.5 shrink-0" aria-label="Main">
          <IconButton label={`Locks (${holdsCount})`} showLabel onClick={() => setState({ locksOpen: true })}>
            <Lock className="w-4 h-4" style={{ color: holdsCount > 0 ? "var(--accent-amber)" : undefined }} />
          </IconButton>
          <IconButton label="Repos" showLabel onClick={() => openPanel("repos")}><FolderGit2 className="w-4 h-4" /></IconButton>
          <IconButton label="Sites" showLabel onClick={() => openPanel("sites")}><Server className="w-4 h-4" /></IconButton>
          <IconButton label="More" showLabel onClick={() => openPanel("more")}><LayoutGrid className="w-4 h-4" /></IconButton>
          <IconButton label="Settings" onClick={() => setState({ settingsOpen: true })}><Settings className="w-4 h-4" /></IconButton>
        </nav>
      )}
    </header>
  );
}
