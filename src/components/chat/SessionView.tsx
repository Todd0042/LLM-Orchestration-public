import { useState } from "react";
import { ArrowLeft, Eye, EyeOff } from "lucide-react";
import { cancelTurn, closeSession, openArtifact, sendSession } from "@/state/actions";
import { useApp } from "@/state/store";
import { IconButton } from "@/components/ui/IconButton";
import { Composer } from "./Composer";
import { LiveTurn } from "./LiveTurn";
import { MessageList } from "./MessageList";

const AGENT_NAME = { agy: "AGY", claude: "Claude Code", orchestration: "Orchestrator" } as const;

/** A single AGY or Claude Code session (from either machine): read it, or keep talking to it. */
export function SessionView({ onBack }: { onBack?: () => void }) {
  const chat = useApp((s) => s.session);
  const live = useApp((s) => (s.session ? s.live[s.session.id] : undefined));
  const [showWork, setShowWork] = useState(false);

  if (!chat) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-center px-6" style={{ color: "var(--text-muted)" }}>
        Pick an AGY or Claude Code session from the list to read it here.
      </div>
    );
  }
  const agent = AGENT_NAME[chat.source];

  return (
    <section className="h-full min-h-0 min-w-0 flex flex-col" aria-label={`${agent} session`} style={{ backgroundColor: "var(--bg-canvas)" }}>
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
        <IconButton label={onBack ? "Back" : "Close session"} onClick={() => (onBack ? onBack() : closeSession())}>
          <ArrowLeft className="w-4 h-4" />
        </IconButton>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold truncate">{chat.title}</div>
          <div className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>{agent} on {chat.nodeId}</div>
        </div>
        <IconButton label={showWork ? "Hide work steps" : "Show work steps"} active={showWork} onClick={() => setShowWork((v) => !v)}>
          {showWork ? <Eye className="w-4 h-4" /> : <EyeOff className="w-4 h-4" />}
        </IconButton>
      </div>
      <MessageList
        messages={chat.messages}
        showWork={showWork}
        onOpenArtifact={openArtifact}
        footer={live ? <LiveTurn turn={live} /> : undefined}
        empty={showWork ? "This session has no messages yet." : "Only work steps so far. Tap the eye to see them."}
      />
      <Composer
        label={`Reply to this ${agent} session`}
        placeholder={`Reply to this ${agent} session on ${chat.nodeId}…`}
        busy={Boolean(live)}
        onSend={(t) => void sendSession(t)}
        onStop={() => void cancelTurn(chat.id)}
      />
    </section>
  );
}
