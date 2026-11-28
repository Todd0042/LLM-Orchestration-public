import { ConversationView } from "@/components/chat/ConversationView";
import { setState, useApp } from "@/state/store";
import { AgentCards } from "./AgentCards";

/**
 * The orchestrator pane. The agent terminals are the orchestrator now; the old chat stays one tap
 * away until the switch-over, when it's removed.
 */
export function OrchestratorView({ wide }: { wide: boolean }) {
  const mode = useApp((s) => s.orchMode);
  const pick = (m: "terminals" | "chat") => {
    localStorage.setItem("orch.mode", m);
    setState({ orchMode: m });
  };
  return (
    <div className="h-full min-h-0 flex flex-col">
      <div className="shrink-0 flex gap-1 px-2 pt-1.5" role="tablist" aria-label="Orchestrator view" style={{ backgroundColor: "var(--bg-panel)" }}>
        {(["terminals", "chat"] as const).map((m) => (
          <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => pick(m)}
                  className="tap rounded-t-md px-3 py-1 text-xs font-semibold cursor-pointer"
                  style={{ backgroundColor: mode === m ? "var(--bg-canvas)" : "transparent", color: mode === m ? "var(--text-primary)" : "var(--text-muted)" }}>
            {m === "terminals" ? "Agent terminals" : "Old chat"}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0">{mode === "terminals" ? <AgentCards wide={wide} /> : <ConversationView wide={wide} />}</div>
    </div>
  );
}
