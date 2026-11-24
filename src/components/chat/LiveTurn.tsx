import { Loader2 } from "lucide-react";
import type { LiveTurn as LiveTurnState } from "@/state/store";

/** What's happening right now in a turn: the last few steps, plus the agent's reply as it's written. */
export function LiveTurn({ turn }: { turn: LiveTurnState }) {
  const steps = turn.steps.slice(-4);
  return (
    <div className="rounded-xl border p-3 space-y-2 text-xs" style={{ borderColor: "var(--accent-primary)", backgroundColor: "var(--accent-primary-soft)" }} data-testid="live-turn">
      <div className="flex items-center gap-2 font-semibold" style={{ color: "var(--accent-primary)" }}>
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
        {steps[steps.length - 1]?.step ?? "Working…"}
      </div>
      {steps.length > 1 && (
        <ol className="space-y-0.5" style={{ color: "var(--text-secondary)" }}>
          {steps.slice(0, -1).map((s, i) => (
            <li key={i} className="truncate">✓ {s.step}{s.detail ? ` · ${s.detail}` : ""}</li>
          ))}
        </ol>
      )}
      {turn.streamText && (
        <div className="whitespace-pre-wrap text-sm leading-relaxed max-h-72 overflow-y-auto" style={{ color: "var(--text-primary)" }}>
          {turn.streamText}
        </div>
      )}
    </div>
  );
}
