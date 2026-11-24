import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, ChevronDown, ChevronRight, Copy, FileDiff, Route, Wrench } from "lucide-react";
import type { ArtifactItem, ChatEvent, ChatMessage } from "@/types/orch";

type OpenArtifact = (a: Partial<ArtifactItem> & { path: string; nodeId: string }) => void;

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      aria-label={done ? "Copied" : "Copy"}
      title="Copy"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[0.6562rem] border cursor-pointer"
      style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-elevated)", color: "var(--text-secondary)" }}
    >
      {done ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function Diff({ ev, onOpen }: { ev: ChatEvent; onOpen: () => void }) {
  const [open, setOpen] = useState(false);
  const name = (ev.path || "").split("/").pop() || "file";
  return (
    <div className="rounded-lg border overflow-hidden text-xs" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
      <div className="flex items-center justify-between gap-2 px-2.5 py-1.5" style={{ backgroundColor: "var(--bg-elevated)" }}>
        <button type="button" onClick={() => setOpen((v) => !v)} className="inline-flex items-center gap-1.5 font-mono-code text-[0.6875rem] truncate cursor-pointer" aria-expanded={open}>
          {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          <FileDiff className="w-3 h-3" style={{ color: "var(--accent-primary)" }} />
          {ev.tool === "Write" ? "Wrote" : "Edited"} <span className="font-semibold" style={{ color: "var(--accent-primary)" }}>{name}</span>
        </button>
        <button type="button" onClick={onOpen} className="text-[0.6562rem] px-2 py-0.5 rounded border cursor-pointer" style={{ borderColor: "var(--border-strong)" }}>
          Open
        </button>
      </div>
      {open && (
        <pre className="p-2 font-mono-code text-[0.6875rem] overflow-x-auto leading-relaxed">
          {(ev.oldText || "").split("\n").slice(0, 40).map((l, i) => l && <div key={`o${i}`} className="bg-red-500/12 text-red-300 px-1">- {l}</div>)}
          {(ev.newText || "").split("\n").slice(0, 40).map((l, i) => l && <div key={`n${i}`} className="bg-emerald-500/12 text-emerald-300 px-1">+ {l}</div>)}
        </pre>
      )}
    </div>
  );
}

function ToolCard({ ev }: { ev: ChatEvent }) {
  return (
    <details className="rounded-lg border px-2.5 py-1.5 text-[0.7188rem]" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-muted)" }}>
      <summary className="cursor-pointer inline-flex items-center gap-1.5 font-mono-code max-w-full">
        <Wrench className="w-3 h-3 shrink-0" style={{ color: "var(--accent-amber)" }} />
        <span className="font-semibold">{ev.tool}</span>
        <span className="truncate" style={{ color: "var(--text-secondary)" }}>{ev.summary}</span>
      </summary>
      <pre className="mt-1.5 text-[0.6875rem] font-mono-code whitespace-pre-wrap break-all max-h-80 overflow-auto" style={{ color: "var(--text-secondary)" }}>
        {ev.detail}
      </pre>
    </details>
  );
}

/** Split "Agent (laptop) · desktop offline; used qwen3-8b on laptop" into a name and a routing note. */
function splitLabel(label: string): { name: string; note: string } {
  const i = label.indexOf(" · ");
  if (i > 0) return { name: label.slice(0, i), note: label.slice(i + 3) };
  const fb = label.match(/^(.*?)\s*\((.*fallback.*)\)$/i);
  if (fb) return { name: fb[1], note: fb[2] };
  return { name: label, note: "" };
}

export function Markdown({ text, nodeId, onOpenArtifact }: { text: string; nodeId: string; onOpenArtifact: OpenArtifact }) {
  return (
    <div className="md max-w-none break-words leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            if (href?.startsWith("file://")) {
              const path = href.replace(/^file:\/\//, "").split("#")[0];
              return (
                <button type="button" onClick={() => onOpenArtifact({ kind: /\.html?$/i.test(path) ? "html" : "file", path, nodeId })}
                        className="inline px-1 rounded font-mono-code text-[0.92em] underline cursor-pointer"
                        style={{ backgroundColor: "var(--accent-primary-soft)", color: "var(--accent-primary)" }}>
                  {children}
                </button>
              );
            }
            return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
          },
          table: ({ children }) => (
            <div className="md-table">
              <table>{children}</table>
            </div>
          ),
          pre: ({ children }) => {
            const code = extractText(children);
            return (
              <div className="md-pre">
                <div className="md-pre-bar"><CopyButton text={code} /></div>
                <pre>{children}</pre>
              </div>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function extractText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node) return extractText((node as { props: { children?: ReactNode } }).props.children);
  return "";
}

/** Hide tool-call chatter: keep prompts and each turn's final answer. */
export function finalOutputsOnly(messages: ChatMessage[]): { visible: ChatMessage[]; hidden: number } {
  const visible: ChatMessage[] = [];
  let hidden = 0;
  let i = 0;
  while (i < messages.length) {
    if (messages[i].role !== "assistant") {
      visible.push(messages[i++]);
      continue;
    }
    const group: ChatMessage[] = [];
    while (i < messages.length && messages[i].role === "assistant") group.push(messages[i++]);
    const withText = group.filter((m) => m.content.trim());
    if (!withText.length) {
      hidden += group.length;
      continue;
    }
    let lastWork = -1;
    group.forEach((m, idx) => {
      if ((m.events || []).some((e) => e.type === "tool" || e.type === "diff")) lastWork = idx;
    });
    const trailing = group.slice(lastWork + 1).filter((m) => m.content.trim());
    const chosen = trailing.length ? trailing : [withText[withText.length - 1]];
    hidden += group.length - chosen.length;
    visible.push(...chosen);
  }
  return { visible, hidden };
}

interface MessageListProps {
  messages: ChatMessage[];
  showWork: boolean;
  onOpenArtifact: OpenArtifact;
  empty: ReactNode;
  footer?: ReactNode;
}

export function MessageList({ messages, showWork, onOpenArtifact, empty, footer }: MessageListProps) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const shown = useMemo(() => (showWork ? messages : finalOutputsOnly(messages).visible), [messages, showWork]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [shown.length, footer]);

  return (
    <div
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
      className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-3 py-3 sm:px-5 space-y-3"
      aria-live="polite"
    >
      {shown.length === 0 && !footer ? (
        <div className="h-full flex items-center justify-center text-sm text-center px-6" style={{ color: "var(--text-muted)" }}>{empty}</div>
      ) : (
        shown.map((m) => {
          const user = m.role === "user";
          const { name, note } = splitLabel(m.senderLabel || (user ? "You" : "Assistant"));
          return (
            <article key={m.id} className={`rounded-xl border p-3 sm:p-3.5 space-y-2 ${user ? "ml-auto max-w-[92%] sm:max-w-[80%]" : ""}`}
                     style={{ backgroundColor: user ? "var(--bg-elevated)" : "var(--bg-panel)", borderColor: user ? "var(--border-strong)" : "var(--border-subtle)" }}>
              <header className="flex items-center gap-1.5 flex-wrap text-[0.7188rem]">
                <span className="font-semibold">{name}</span>
                {m.nodeId && <span className="font-mono-code px-1.5 rounded" style={{ backgroundColor: "var(--bg-muted)", color: "var(--text-muted)" }}>{m.nodeId}</span>}
                {m.modelId && !user && <span className="font-mono-code" style={{ color: "var(--accent-primary)" }}>{m.modelId}</span>}
                {note && (
                  <span className="inline-flex items-center gap-1 px-1.5 rounded" style={{ backgroundColor: "rgba(245,158,11,0.12)", color: "var(--accent-amber)" }} title="Where this ran and why">
                    <Route className="w-3 h-3" /> {note}
                  </span>
                )}
                <span className="ml-auto" style={{ color: "var(--text-muted)" }}>{formatTime(m.createdAt)}</span>
              </header>
              {showWork &&
                m.events?.map((ev, i) =>
                  ev.type === "diff" ? (
                    <Diff key={i} ev={ev} onOpen={() => ev.path && onOpenArtifact({ kind: "diff", path: ev.path, nodeId: m.nodeId, meta: { oldText: ev.oldText, newText: ev.newText } })} />
                  ) : ev.type === "tool" ? (
                    <ToolCard key={i} ev={ev} />
                  ) : ev.type === "thinking" && ev.text ? (
                    <details key={i} className="rounded-lg border px-2.5 py-1.5 text-[0.7188rem]" style={{ borderColor: "var(--border-subtle)" }}>
                      <summary className="cursor-pointer" style={{ color: "var(--text-muted)" }}>Reasoning</summary>
                      <div className="mt-1 whitespace-pre-wrap" style={{ color: "var(--text-secondary)" }}>{ev.text}</div>
                    </details>
                  ) : ev.type === "media" && ev.path ? (
                    <button key={i} type="button" onClick={() => onOpenArtifact({ kind: ev.kind || "image", path: ev.path!, nodeId: ev.nodeId || m.nodeId })}
                            className="text-xs underline cursor-pointer" style={{ color: "var(--accent-primary)" }}>
                      Open {ev.kind} · {ev.filename}
                    </button>
                  ) : null
                )}
              {m.content && <Markdown text={m.content} nodeId={m.nodeId} onOpenArtifact={onOpenArtifact} />}
            </article>
          );
        })
      )}
      {footer}
    </div>
  );
}

function formatTime(ms: string): string {
  const n = Number(ms);
  if (!n) return "";
  const d = new Date(n);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" });
}
