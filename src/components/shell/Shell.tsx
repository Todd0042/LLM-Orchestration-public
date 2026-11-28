import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { ChatList } from "@/components/chat/ChatList";
import { OrchestratorView } from "@/components/orch/OrchestratorView";
import { SessionView } from "@/components/chat/SessionView";
import { ArtifactsAndViewer } from "@/components/ArtifactsAndViewer";
import { MoreView } from "@/components/ops/MoreView";
import { ReposView } from "@/components/ops/ReposView";
import { SitesView } from "@/components/ops/SitesView";
import { Sheet } from "@/components/ui/Sheet";
import { closeSession, openPanel } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { BottomNav } from "./BottomNav";
import { Inspector } from "./Inspector";
import { TopBar } from "./TopBar";

const WIDE_QUERY = "(min-width: 1024px)";

export function useWide(): boolean {
  const [wide, setWide] = useState(() => window.matchMedia(WIDE_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(WIDE_QUERY);
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

const PANEL_TITLES = { repos: "Repos on every machine", sites: "Sites & services", activity: "Activity log", more: "More" } as const;

/** Wide: list | conversation | inspector, with Repos/Sites/More in a side sheet. Narrow: one view + bottom tabs. */
export function Shell() {
  const wide = useWide();
  const view = useApp((s) => s.view);
  const panel = useApp((s) => s.panel);
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  const session = useApp((s) => s.session);
  const artifact = useApp((s) => s.artifact);
  const inspector = useApp((s) => s.inspector);

  return (
    <div className="h-full w-full flex flex-col overflow-hidden">
      <TopBar wide={wide} />
      <main className="flex-1 min-h-0">
        {wide ? (
          <PanelGroup direction="horizontal" autoSaveId="orch-layout-v2">
            <Panel defaultSize={20} minSize={14} maxSize={32}>
              <ChatList />
            </Panel>
            <PanelResizeHandle className="w-1 hover:bg-sky-500/40 transition-colors" style={{ backgroundColor: "var(--border-subtle)" }} />
            <Panel defaultSize={46} minSize={30}>
              <OrchestratorView wide />
            </Panel>
            <PanelResizeHandle className="w-1 hover:bg-sky-500/40 transition-colors" style={{ backgroundColor: "var(--border-subtle)" }} />
            <Panel defaultSize={34} minSize={22}>
              <Inspector />
            </Panel>
          </PanelGroup>
        ) : (
          <div className="h-full min-h-0">
            {view === "chat" && <OrchestratorView wide={false} />}
            {view === "sessions" && (session ? <SessionView onBack={closeSession} /> : <ChatList mode="sessions" />)}
            {view === "repos" && <div className="h-full overflow-y-auto"><ReposView /></div>}
            {view === "sites" && <div className="h-full overflow-y-auto"><SitesView /></div>}
            {view === "more" && <div className="h-full overflow-y-auto"><MoreView /></div>}
          </div>
        )}
      </main>
      {!wide && <BottomNav />}

      {wide && panel && (
        <Sheet open title={PANEL_TITLES[panel]} onClose={() => openPanel(null)}>
          {panel === "repos" && <ReposView />}
          {panel === "sites" && <SitesView />}
          {panel === "more" && <MoreView />}
        </Sheet>
      )}
      {!wide && (
        <Sheet open={sidebarOpen} side="left" widthClass="w-[min(24rem,88vw)]" title="Conversations" onClose={() => setState({ sidebarOpen: false })}>
          <ChatList mode="conversations" />
        </Sheet>
      )}
      {!wide && artifact && inspector === "artifact" && (
        <Sheet open side="full" title={artifact.title} onClose={() => setState({ inspector: "session" })}>
          <div className="h-full min-h-[60dvh]">
            <ArtifactsAndViewer artifacts={[artifact]} selectedArtifact={artifact} onSelectArtifact={(a) => setState({ artifact: a })} />
          </div>
        </Sheet>
      )}
    </div>
  );
}
