import { useRef, useState, type ReactNode } from "react";
import { ArrowUp, Square } from "lucide-react";

interface ComposerProps {
  placeholder: string;
  busy: boolean;
  onSend: (text: string) => void;
  onStop?: () => void;
  tone?: "primary" | "amber";
  toolbar?: ReactNode;
  label: string;
}

/** Enter sends, Shift+Enter adds a line; grows with its content; Stop replaces Send while a turn runs. */
export function Composer({ placeholder, busy, onSend, onStop, tone = "primary", toolbar, label }: ComposerProps) {
  const [draft, setDraft] = useState("");
  const area = useRef<HTMLTextAreaElement | null>(null);
  const color = tone === "amber" ? "var(--accent-amber)" : "var(--accent-primary)";

  const send = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    if (area.current) area.current.style.height = "";
    onSend(text);
  };

  return (
    <div className="shrink-0 border-t px-2.5 pt-2 space-y-2" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)", paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}>
      {toolbar && <div className="flex items-start gap-1.5">{toolbar}</div>}
      <div className="rounded-xl border p-1.5 pl-3 flex items-end gap-2" style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-strong)" }}>
        <textarea
          ref={area}
          rows={1}
          aria-label={label}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            e.target.style.height = "";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 220)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={placeholder}
          className="flex-1 bg-transparent text-sm outline-none resize-none leading-relaxed py-1.5 max-h-[13.75rem]"
        />
        {busy && onStop ? (
          <button type="button" onClick={onStop} aria-label="Stop" title="Stop" className="tap rounded-lg inline-flex items-center justify-center cursor-pointer border"
                  style={{ borderColor: "var(--accent-crimson)", color: "var(--accent-crimson)" }}>
            <Square className="w-4 h-4" />
          </button>
        ) : (
          <button type="button" onClick={send} disabled={busy || !draft.trim()} aria-label="Send" title="Send"
                  className="tap rounded-lg inline-flex items-center justify-center cursor-pointer disabled:opacity-40" style={{ backgroundColor: color, color: "#0b0e14" }}>
            <ArrowUp className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
}
