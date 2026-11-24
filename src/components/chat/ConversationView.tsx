import { useState } from "react";
import { Eye, EyeOff, Menu, Pencil, Terminal } from "lucide-react";
import { buildOrchestratorOptions } from "@/lib/modelSort";
import { cancelTurn, openArtifact, renameChat, runScript, sendMain, startRelay } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { Dropdown } from "@/components/ui/Dropdown";
import { IconButton } from "@/components/ui/IconButton";
import { Composer } from "./Composer";
import { LiveTurn } from "./LiveTurn";
import { MessageList } from "./MessageList";
import { ModelPicker } from "./ModelPicker";

const SUGGESTIONS = [
  "Which of my repos are out of sync between the machines?",
  "Are all my sites healthy right now?",
  "Ask Claude on the desktop to review the last commit in this repo",
];

/** The orchestrator conversation: the thing you always talk to. */
export function ConversationView({ wide }: { wide: boolean }) {
  const chat = useApp((s) => s.mainChat);
  const live = useApp((s) => (s.mainChat ? s.live[s.mainChat.id] : undefined));
  const local = useApp((s) => s.settings?.node.id ?? "desktop");
  const peers = useApp((s) => s.peersStatus);
  const [showWork, setShowWork] = useState(false);
  const [script, setScript] = useState(false);
  const [scriptNode, setScriptNode] = useState("local");
  const [editing, setEditing] = useState(false);
  const busy = Boolean(live);

  return (
    <section className="h-full min-h-0 min-w-0 flex flex-col" aria-label="Conversation" style={{ backgroundColor: "var(--bg-canvas)" }}>
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
        {!wide && (
          <IconButton label="Conversations and sessions" onClick={() => setState({ sidebarOpen: true })}>
            <Menu className="w-4 h-4" />
          </IconButton>
        )}
        {editing && chat ? (
          <form
            className="flex-1 min-w-0"
            onSubmit={(e) => {
              e.preventDefault();
              const v = new FormData(e.currentTarget).get("title");
              setEditing(false);
              if (typeof v === "string" && v.trim()) void renameChat(chat.id, v.trim());
            }}
          >
            <input name="title" defaultValue={chat.title} aria-label="Conversation name" autoFocus onBlur={(e) => e.currentTarget.form?.requestSubmit()}
                   className="w-full rounded-md border px-2 py-1 text-sm bg-transparent" style={{ borderColor: "var(--accent-primary)" }} />
          </form>
        ) : (
          <button type="button" className="flex-1 min-w-0 text-left text-sm font-semibold truncate inline-flex items-center gap-1.5 cursor-pointer"
                  onClick={() => chat && setEditing(true)} title={chat ? "Rename" : undefined}>
            <span className="truncate">{chat?.title ?? "New conversation"}</span>
            {chat && <Pencil className="w-3 h-3 shrink-0 opacity-50" />}
          </button>
        )}
        <IconButton label={showWork ? "Hide work steps" : "Show work steps"} active={showWork} onClick={() => setShowWork((v) => !v)}>
          {showWork ? <Eye className="w-4 h-4" /> : <EyeOff className="w-4 h-4" />}
        </IconButton>
      </div>

      {chat?.mode === "relay" && <RelayBar />}

      <MessageList
        messages={chat?.messages ?? []}
        showWork={showWork}
        onOpenArtifact={openArtifact}
        footer={live ? <LiveTurn turn={live} /> : undefined}
        empty={
          <div className="space-y-3 max-w-md">
            <p className="text-base" style={{ color: "var(--text-secondary)" }}>What should we work on?</p>
            <div className="flex flex-col gap-2">
              {SUGGESTIONS.map((t) => (
                <button key={t} type="button" onClick={() => void sendMain(t)} className="rounded-lg border px-3 py-2 text-left text-sm cursor-pointer"
                        style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)", color: "var(--text-primary)" }}>
                  {t}
                </button>
              ))}
            </div>
          </div>
        }
      />

      <Composer
        label={script ? "Command to run" : "Message"}
        placeholder={script ? `Run a command on ${scriptNode === "local" ? local : scriptNode}…` : "Message the orchestrator…"}
        busy={busy}
        tone={script ? "amber" : "primary"}
        onSend={(t) => void (script ? runScript(t, scriptNode) : sendMain(t))}
        onStop={chat ? () => void cancelTurn(chat.id) : undefined}
        toolbar={
          <>
            <div className="flex-1 min-w-0 flex flex-wrap items-center gap-1.5">
            {script ? (
              <Dropdown
                title="Run on"
                value={scriptNode}
                onChange={setScriptNode}
                placement="up"
                options={[{ value: "local", label: `Run on ${local}` }, ...peers.map((p) => ({ value: p.id, label: `Run on ${p.id}${p.online ? "" : " (offline)"}` }))]}
              />
            ) : (
              <ModelPicker />
            )}
            </div>
            <IconButton label={script ? "Back to chat" : "Run a command"} showLabel={wide} active={script} onClick={() => setScript((v) => !v)} className="shrink-0">
              <Terminal className="w-4 h-4" />
            </IconButton>
          </>
        }
      />
    </section>
  );
}

function RelayBar() {
  const models = useApp((s) => s.models);
  const local = useApp((s) => s.settings?.node.id ?? "desktop");
  const [a, setA] = useState(`${local}::agy-cli`);
  const [b, setB] = useState(`${local}::claude-cli`);
  const [topic, setTopic] = useState("");
  const opts = buildOrchestratorOptions(models, local, { includeHint: false });
  return (
    <form
      className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-2 border-b text-xs"
      style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-muted)" }}
      onSubmit={(e) => {
        e.preventDefault();
        if (!topic.trim()) return;
        const [nodeA, modelA] = a.split("::");
        const [nodeB, modelB] = b.split("::");
        void startRelay({ topic, turns: 6, nodeA, modelA, roleA: "Architect & Coder", nodeB, modelB, roleB: "Reviewer & QA" });
        setTopic("");
      }}
    >
      <span className="font-semibold" style={{ color: "var(--accent-primary)" }}>Relay</span>
      <Dropdown title="First agent" value={a} onChange={setA} options={opts} menuWidthClass="min-w-[15.0rem]" />
      <span aria-hidden="true">↔</span>
      <Dropdown title="Second agent" value={b} onChange={setB} options={opts} menuWidthClass="min-w-[15.0rem]" />
      <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="What should they work on?" aria-label="Relay topic"
             className="flex-1 min-w-[11.25rem] rounded border px-2 py-1.5 bg-transparent" style={{ borderColor: "var(--border-subtle)" }} />
      <button type="submit" className="tap px-3 rounded font-semibold cursor-pointer" style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>Start</button>
    </form>
  );
}
