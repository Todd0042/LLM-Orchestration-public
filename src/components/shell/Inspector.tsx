import { useMemo } from "react";
import { ArtifactsAndViewer } from "@/components/ArtifactsAndViewer";
import { SessionView } from "@/components/chat/SessionView";
import { setState, useApp } from "@/state/store";

/** Right-hand pane on wide screens: the agent session you picked, or files/diffs/media from either chat. */
export function Inspector() {
  const tab = useApp((s) => s.inspector);
  const session = useApp((s) => s.session);
  const main = useApp((s) => s.mainChat);
  const artifact = useApp((s) => s.artifact);
  const artifacts = useMemo(() => {
    const all = [...(session?.artifacts ?? []), ...(main?.artifacts ?? [])];
    if (artifact && !all.some((a) => a.id === artifact.id)) all.unshift(artifact);
    return all;
  }, [session, main, artifact]);

  return (
    <aside className="h-full min-h-0 flex flex-col" aria-label="Inspector" style={{ backgroundColor: "var(--bg-canvas)" }}>
      <div role="tablist" className="shrink-0 flex gap-1 px-2 pt-1.5 border-b" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
        {(["session", "artifact"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} type="button" onClick={() => setState({ inspector: t })}
                  className="px-3 py-2 text-xs font-medium border-b-2 cursor-pointer"
                  style={{ borderColor: tab === t ? "var(--accent-primary)" : "transparent", color: tab === t ? "var(--accent-primary)" : "var(--text-secondary)" }}>
            {t === "session" ? "Agent session" : `Files & diffs${artifacts.length ? ` (${artifacts.length})` : ""}`}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0">
        {tab === "session" ? (
          <SessionView />
        ) : (
          <ArtifactsAndViewer artifacts={artifacts} selectedArtifact={artifact} onSelectArtifact={(a) => setState({ artifact: a })} />
        )}
      </div>
    </aside>
  );
}
