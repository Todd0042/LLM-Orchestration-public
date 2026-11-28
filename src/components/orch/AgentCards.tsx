import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowRightLeft, FileText, GitMerge, Monitor, OctagonX, Play, Plus, RotateCcw, Send, ShieldCheck, X } from "lucide-react";
import { fetchRepoInventory } from "@/lib/api";
import {
  SLOT_ORDER, closeCard, errorText, fetchCards, fetchContext, handOff, interruptHolder, mergeToMain, openCard, openInAlacritty,
  protectMain, sendLine, slotAction, slotSummary, type OrchCard, type SlotId,
} from "@/lib/orchTerminal";
import { toast } from "@/state/actions";
import { useApp } from "@/state/store";
import { IconButton } from "@/components/ui/IconButton";
import { Sheet } from "@/components/ui/Sheet";
import { TerminalPane } from "./TerminalPane";

const STATE_COLOR = (card: OrchCard, slot: SlotId) => {
  const s = card.slots[slot];
  if (s.outUntil) return "var(--accent-amber)";
  if (s.state !== "running") return "var(--text-muted)";
  return s.busy ? "var(--accent-emerald)" : "var(--text-secondary)";
};

/** The orchestrator: one card per repo, three agent terminals per card, one baton. */
export function AgentCards({ wide }: { wide: boolean }) {
  const tick = useApp((s) => s.orchTick);
  const zone = useApp((s) => s.auth?.zone ?? "local");
  const [cards, setCards] = useState<OrchCard[] | null>(null);
  const [current, setCurrent] = useState<string>(() => localStorage.getItem("orch.card") || "");
  const [opening, setOpening] = useState(false);

  const refresh = useCallback(() => {
    fetchCards().then(setCards).catch(() => undefined);
  }, []);
  useEffect(() => refresh(), [refresh, tick]);
  useEffect(() => {
    // busy/idle of AGY and opencode comes from screen activity, which has no event: poll while shown
    const t = window.setInterval(refresh, 2500);
    return () => window.clearInterval(t);
  }, [refresh]);

  const card = cards?.find((c) => c.id === current) ?? cards?.[0];
  useEffect(() => {
    if (card) localStorage.setItem("orch.card", card.id);
  }, [card]);

  return (
    <section className="h-full min-h-0 flex flex-col" aria-label="Orchestrator agents" style={{ backgroundColor: "var(--bg-canvas)" }}>
      <div className="shrink-0 flex items-center gap-1.5 px-2 py-1.5 border-b overflow-x-auto" role="tablist" aria-label="Repos"
           style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
        {(cards ?? []).map((c) => (
          <button key={c.id} type="button" role="tab" aria-selected={c.id === card?.id} onClick={() => setCurrent(c.id)}
                  className="tap shrink-0 rounded-md px-2.5 py-1 text-xs font-semibold cursor-pointer"
                  style={{ backgroundColor: c.id === card?.id ? "var(--accent-primary-soft)" : "transparent",
                           color: c.id === card?.id ? "var(--accent-primary)" : "var(--text-secondary)" }}>
            {c.repo}
          </button>
        ))}
        {zone !== "tunnel" && (
          <IconButton label="Open agents for a repo" showLabel={!cards?.length} onClick={() => setOpening(true)}>
            <Plus className="w-4 h-4" />
          </IconButton>
        )}
      </div>

      {cards === null ? (
        <p className="p-4 text-sm" style={{ color: "var(--text-muted)" }}>Loading…</p>
      ) : !card ? (
        <EmptyState zone={zone} onOpen={() => setOpening(true)} />
      ) : (
        <CardView key={card.id} card={card} wide={wide} zone={zone} onChanged={refresh} onClosed={() => { setCurrent(""); refresh(); }} />
      )}

      {opening && <OpenRepoSheet onClose={() => setOpening(false)} onOpened={(c) => { setOpening(false); setCurrent(c.id); refresh(); }} />}
    </section>
  );
}

function EmptyState({ zone, onOpen }: { zone: string; onOpen: () => void }) {
  return (
    <div className="p-6 max-w-lg space-y-3 text-sm" style={{ color: "var(--text-secondary)" }}>
      <p className="text-base" style={{ color: "var(--text-primary)" }}>No agents running yet.</p>
      <p>
        Pick a repo and Claude starts in a real terminal there, on the <code>orchestrator-modified</code> branch. AGY and a local
        model wait beside it; only one works at a time, and you move the baton.
      </p>
      {zone === "tunnel" ? (
        <p>Over the internet the terminals are watch-only. Start them from the desktop or laptop, then drive Claude here through the Claude app's Remote Control.</p>
      ) : (
        <button type="button" onClick={onOpen} className="tap rounded-md px-3 py-2 font-semibold cursor-pointer"
                style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>
          Open agents for a repo
        </button>
      )}
    </div>
  );
}

function OpenRepoSheet({ onClose, onOpened }: { onClose: () => void; onOpened: (c: OrchCard) => void }) {
  const [repos, setRepos] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    fetchRepoInventory().then((r) => setRepos([...new Set(r.localRepos.map((i) => i.repoName))].sort((a, b) => a.localeCompare(b)))).catch(() => undefined);
  }, []);
  const shown = repos.filter((r) => r.toLowerCase().includes(filter.toLowerCase()));
  return (
    <Sheet open side="center" title="Open agents for a repo" onClose={onClose}>
      <div className="space-y-2 p-1">
        <input autoFocus value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter repos" aria-label="Filter repos"
               className="w-full rounded-md border px-2 py-1.5 text-sm bg-transparent" style={{ borderColor: "var(--border-subtle)" }} />
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          The repo switches to <code>orchestrator-modified</code> (created from main if needed). Uncommitted changes must be committed or stashed first.
        </p>
        <ul className="max-h-[50dvh] overflow-y-auto divide-y" style={{ borderColor: "var(--border-subtle)" }}>
          {shown.map((r) => (
            <li key={r}>
              <button type="button" disabled={!!busy} className="tap w-full text-left px-2 py-2 text-sm cursor-pointer disabled:opacity-50"
                      onClick={() => {
                        setBusy(r);
                        openCard(r).then(onOpened).catch((e) => toast(errorText(e), "error")).finally(() => setBusy(""));
                      }}>
                {r}{busy === r ? " — starting Claude…" : ""}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </Sheet>
  );
}

function CardView({ card, wide, zone, onChanged, onClosed }: { card: OrchCard; wide: boolean; zone: string; onChanged: () => void; onClosed: () => void }) {
  const [viewing, setViewing] = useState<SlotId>(card.holder);
  const [contextOpen, setContextOpen] = useState(false);
  const local = zone === "local";
  const remote = zone === "tunnel";
  useEffect(() => setViewing(card.holder), [card.holder]);

  const act = (p: Promise<unknown>, done?: string) =>
    p.then(() => { if (done) toast(done); onChanged(); }).catch((e) => toast(errorText(e), "error"));
  const holder = card.slots[card.holder];
  const others = SLOT_ORDER.filter((s) => s !== viewing);
  const canType = !remote && viewing === card.holder && !card.handoff;

  const actions = (
    <div className="flex flex-wrap items-center gap-1.5">
      <IconButton label="Shared notes (.orch/context.md)" showLabel={wide} onClick={() => setContextOpen(true)}><FileText className="w-4 h-4" /></IconButton>
      {!remote && holder.state === "running" && (
        <IconButton label={`Interrupt ${card.labels[card.holder]}`} showLabel={wide} onClick={() => void act(interruptHolder(card.id))}>
          <OctagonX className="w-4 h-4" />
        </IconButton>
      )}
      {!remote && (
        <IconButton label="Merge to main" showLabel={wide} onClick={() => {
          if (confirm(`Merge ${card.branch} into main in ${card.repo} and push it? The branch then starts fresh from main.`))
            void act(mergeToMain(card.id), "Merged to main");
        }}><GitMerge className="w-4 h-4" /></IconButton>
      )}
      {!remote && (
        <IconButton label="Protect main on GitHub" showLabel={wide} onClick={() => void act(protectMain(card.id), "main is protected on GitHub (you can still push as admin)")}>
          <ShieldCheck className="w-4 h-4" />
        </IconButton>
      )}
      {local && wide && (
        <IconButton label="Open in Alacritty" showLabel onClick={() => void act(openInAlacritty(card.id, viewing))}><Monitor className="w-4 h-4" /></IconButton>
      )}
      {!remote && (
        <IconButton label="Close these agents" onClick={() => {
          if (confirm(`Stop all three agents for ${card.repo}? Their conversations can be resumed later from the agents themselves.`))
            closeCard(card.id).then(onClosed).catch((e) => toast(errorText(e), "error"));
        }}><X className="w-4 h-4" /></IconButton>
      )}
    </div>
  );

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-2 border-b" style={{ borderColor: "var(--border-subtle)" }}>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold truncate">{card.repo}</div>
          <div className="text-xs truncate" style={{ color: "var(--text-muted)" }}>
            branch <code>{card.branch}</code> · baton: <strong style={{ color: "var(--accent-primary)" }}>{card.labels[card.holder]}</strong>
            {remote ? " · watch-only over the internet" : ""}
          </div>
        </div>
        {actions}
      </div>

      <Banners card={card} remote={remote} act={act} />

      {wide ? (
        <div className="flex-1 min-h-0 flex flex-col">
          <SlotHeader card={card} slot={viewing} canType={canType} remote={remote} act={act} />
          <div className="flex-1 min-h-0"><SlotBody card={card} slot={viewing} canType={canType} remote={remote} act={act} /></div>
          {others.map((s) => (
            <Strip key={s} card={card} slot={s} onView={() => setViewing(s)} remote={remote} act={act} />
          ))}
        </div>
      ) : (
        <div className="flex-1 min-h-0 flex flex-col">
          <div className="shrink-0 grid grid-cols-3 border-b" role="tablist" aria-label="Agents" style={{ borderColor: "var(--border-subtle)" }}>
            {SLOT_ORDER.map((s) => (
              <button key={s} type="button" role="tab" aria-selected={viewing === s} onClick={() => setViewing(s)}
                      className="tap h-11 text-xs font-semibold cursor-pointer"
                      style={{ color: viewing === s ? "var(--accent-primary)" : STATE_COLOR(card, s),
                               borderBottom: viewing === s ? "2px solid var(--accent-primary)" : "2px solid transparent" }}>
                {card.labels[s]}{card.holder === s ? " ●" : ""}
              </button>
            ))}
          </div>
          <SlotHeader card={card} slot={viewing} canType={canType} remote={remote} act={act} />
          <div className="flex-1 min-h-0"><SlotBody card={card} slot={viewing} canType={canType} remote={remote} act={act} /></div>
          {!remote && <PhoneInput card={card} act={act} />}
        </div>
      )}

      {contextOpen && <ContextSheet card={card} onClose={() => setContextOpen(false)} />}
    </div>
  );
}

type Act = (p: Promise<unknown>, done?: string) => Promise<void>;

function Banners({ card, remote, act }: { card: OrchCard; remote: boolean; act: Act }) {
  const h = card.handoff;
  const out = card.slots[card.holder].outUntil ? card.slots[card.holder] : null;
  if (h) {
    const text = h.phase === "failed" ? `Handoff to ${card.labels[h.to]} failed: ${h.error}`
      : h.phase === "updating" ? `${card.labels[h.from]} is updating the shared notes before handing over to ${card.labels[h.to]}…`
      : `Handing the baton to ${card.labels[h.to]}…`;
    return <Banner tone={h.phase === "failed" ? "error" : "info"}>{text}</Banner>;
  }
  if (out) {
    return (
      <Banner tone="warn">
        <span>{card.labels[card.holder]} is out of usage until {out.outText}.</span>
        {!remote && SLOT_ORDER.filter((s) => s !== card.holder && !card.slots[s].outUntil).map((s) => (
          <button key={s} type="button" onClick={() => void act(handOff(card.id, s))} className="tap ml-2 rounded px-2 py-0.5 text-xs font-semibold cursor-pointer"
                  style={{ backgroundColor: "var(--accent-amber)", color: "#0b0e14" }}>
            Hand to {card.labels[s]}
          </button>
        ))}
      </Banner>
    );
  }
  return null;
}

function Banner({ tone, children }: { tone: "info" | "warn" | "error"; children: React.ReactNode }) {
  const color = tone === "error" ? "var(--accent-crimson)" : tone === "warn" ? "var(--accent-amber)" : "var(--accent-primary)";
  return (
    <div role={tone === "error" ? "alert" : "status"} className="shrink-0 flex flex-wrap items-center gap-1 px-3 py-1.5 text-xs border-b"
         style={{ borderColor: color, color, backgroundColor: "var(--bg-muted)" }}>
      {children}
    </div>
  );
}

function SlotHeader({ card, slot, canType, remote, act }: { card: OrchCard; slot: SlotId; canType: boolean; remote: boolean; act: Act }) {
  const isHolder = card.holder === slot;
  return (
    <div className="shrink-0 flex items-center gap-2 px-3 py-1 text-xs" style={{ backgroundColor: "var(--bg-panel)" }}>
      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: STATE_COLOR(card, slot) }} aria-hidden="true" />
      <span className="font-semibold">{slotSummary(card, slot)}</span>
      <span className="ml-auto rounded px-1.5 py-0.5" style={{ backgroundColor: canType ? "var(--accent-primary-soft)" : "var(--bg-muted)",
                                                             color: canType ? "var(--accent-primary)" : "var(--text-muted)" }}>
        {canType ? "You can type" : remote ? "Watching (internet)" : isHolder ? "Watching" : "Watching · no baton"}
      </span>
      {!remote && !isHolder && card.slots[slot].state !== "stopped" && !card.handoff && (
        <button type="button" onClick={() => void act(handOff(card.id, slot))} className="tap inline-flex items-center gap-1 rounded px-2 py-0.5 font-semibold cursor-pointer"
                style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>
          <ArrowRightLeft className="w-3 h-3" /> Hand baton here
        </button>
      )}
    </div>
  );
}

function SlotBody({ card, slot, canType, remote, act }: { card: OrchCard; slot: SlotId; canType: boolean; remote: boolean; act: Act }) {
  const s = card.slots[slot];
  if (s.state === "stopped") {
    return (
      <div className="h-full grid place-items-center text-sm" style={{ color: "var(--text-muted)" }}>
        {remote ? `${card.labels[slot]} isn't running.` : (
          <button type="button" onClick={() => void act(slotAction(card.id, slot, "start"))} className="tap inline-flex items-center gap-1.5 rounded-md px-3 py-2 font-semibold cursor-pointer"
                  style={{ backgroundColor: "var(--bg-elevated)", color: "var(--text-primary)" }}>
            <Play className="w-4 h-4" /> Start {card.labels[slot]}
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="relative h-full">
      <TerminalPane cardId={card.id} slot={slot} canType={canType} label={card.labels[slot]} />
      {s.state === "exited" && !remote && (
        // Never a bare shell: an ended agent can only be resumed or started fresh.
        <div className="absolute inset-x-0 bottom-0 flex flex-wrap items-center gap-2 px-3 py-2 text-sm" style={{ backgroundColor: "var(--bg-elevated)" }}>
          <span>{card.labels[slot]} ended.</span>
          <button type="button" onClick={() => void act(slotAction(card.id, slot, "resume"))} className="tap inline-flex items-center gap-1 rounded px-2 py-1 font-semibold cursor-pointer"
                  style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}>
            <RotateCcw className="w-3.5 h-3.5" /> Resume
          </button>
          <button type="button" onClick={() => void act(slotAction(card.id, slot, "new"))} className="tap rounded px-2 py-1 cursor-pointer"
                  style={{ backgroundColor: "var(--bg-muted)" }}>
            New session
          </button>
        </div>
      )}
    </div>
  );
}

/** A terminal that isn't in front: one line, not a squashed terminal (TUIs garble below ~5 rows). */
function Strip({ card, slot, onView, remote, act }: { card: OrchCard; slot: SlotId; onView: () => void; remote: boolean; act: Act }) {
  const s = card.slots[slot];
  return (
    <div className="shrink-0 flex items-center gap-2 px-3 h-9 border-t text-xs" data-testid={`strip-${slot}`} style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}>
      <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: STATE_COLOR(card, slot) }} aria-hidden="true" />
      <button type="button" onClick={onView} className="tap min-w-0 flex-1 text-left truncate cursor-pointer" title={`Show ${card.labels[slot]}`}>
        <strong>{slotSummary(card, slot)}</strong>
        {card.holder === slot && " · holds the baton"}
        {s.lastLine && <span style={{ color: "var(--text-muted)" }}> · last: “{s.lastLine.slice(0, 80)}”</span>}
      </button>
      {!remote && s.state === "stopped" && (
        <button type="button" onClick={() => void act(slotAction(card.id, slot, "start"))} className="tap rounded px-2 py-0.5 cursor-pointer" style={{ backgroundColor: "var(--bg-elevated)" }}>
          Start
        </button>
      )}
    </div>
  );
}

function PhoneInput({ card, act }: { card: OrchCard; act: Act }) {
  const [text, setText] = useState("");
  return (
    <form className="shrink-0 flex items-center gap-2 px-2 py-2 border-t" style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-panel)" }}
          onSubmit={(e) => {
            e.preventDefault();
            if (!text.trim()) return;
            void act(sendLine(card.id, text));
            setText("");
          }}>
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder={`Type to ${card.labels[card.holder]}…`} aria-label={`Send a line to ${card.labels[card.holder]}`}
             className="flex-1 min-w-0 rounded-md border px-2 h-11 text-sm bg-transparent" style={{ borderColor: "var(--border-subtle)" }} />
      <IconButton label="Send" className="h-11 w-11" onClick={(e) => e.currentTarget.form?.requestSubmit()}><Send className="w-4 h-4" /></IconButton>
    </form>
  );
}

function ContextSheet({ card, onClose }: { card: OrchCard; onClose: () => void }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    fetchContext(card.id).then(setText).catch((e) => setText(errorText(e)));
  }, [card.id]);
  const lines = useMemo(() => text ?? "Loading…", [text]);
  return (
    <Sheet open side="right" title={`${card.repo}: shared notes`} onClose={onClose} widthClass="w-[min(44rem,96vw)]">
      <pre className="whitespace-pre-wrap text-xs p-2 leading-relaxed">{lines}</pre>
    </Sheet>
  );
}
