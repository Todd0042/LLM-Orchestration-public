/** The orchestrator card: Claude, AGY and a local agent in tmux terminals (server/orch_terminal.py). */
import { apiJson } from "@/lib/api";

export type SlotId = "claude" | "agy" | "local";
export const SLOT_ORDER: SlotId[] = ["claude", "agy", "local"];

export interface SlotStatus {
  state: "running" | "exited" | "stopped";
  busy: boolean;
  outUntil: number;
  outText: string;
  outReason: string;
  lastLine: string;
}

export interface OrchCard {
  id: string;
  repo: string;
  workdir: string;
  branch: string;
  holder: SlotId;
  slots: Record<SlotId, SlotStatus>;
  handoff: { from: SlotId; to: SlotId; phase: "starting" | "updating" | "passing" | "failed"; error: string } | null;
  labels: Record<SlotId, string>;
  attachCommand: string;
}

const post = <T,>(path: string, body?: unknown) =>
  apiJson<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const base = (cid: string) => `/api/orch-terminal/cards/${encodeURIComponent(cid)}`;

export const fetchCards = () => apiJson<{ cards: OrchCard[] }>("/api/orch-terminal/cards").then((r) => r.cards);
export const openCard = (repo: string) => post<OrchCard>("/api/orch-terminal/cards", { repo });
export const closeCard = (cid: string) => apiJson<{ ok: boolean }>(base(cid), { method: "DELETE" });
export const slotAction = (cid: string, slot: SlotId, action: "start" | "resume" | "new") => post<SlotStatus>(`${base(cid)}/slots/${slot}/${action}`);
export const sendLine = (cid: string, text: string) => post<{ ok: boolean }>(`${base(cid)}/send`, { text });
export const interruptHolder = (cid: string) => post<{ ok: boolean }>(`${base(cid)}/interrupt`);
export const handOff = (cid: string, to: SlotId) => post<{ status: string }>(`${base(cid)}/handoff`, { to });
export const mergeToMain = (cid: string) => post<{ merged: number; base: string; pushed: boolean; archived: string }>(`${base(cid)}/merge`);
export const protectMain = (cid: string) => post<{ repo: string; branch: string }>(`${base(cid)}/protect`);
export const openInAlacritty = (cid: string, slot: SlotId) => post<{ ok: boolean }>(`${base(cid)}/slots/${slot}/alacritty`);
export const fetchContext = (cid: string) => apiJson<{ text: string }>(`${base(cid)}/context`).then((r) => r.text);

/** "Claude · working", "AGY · out until 9 PM", ... */
export function slotSummary(card: OrchCard, slot: SlotId): string {
  const s = card.slots[slot];
  const parts = [card.labels[slot]];
  if (s.state === "stopped") parts.push("not started");
  else if (s.state === "exited") parts.push("ended");
  else parts.push(s.busy ? "working" : "idle");
  if (s.outUntil) parts.push(`out until ${s.outText}`);
  return parts.join(" · ");
}

/** Server error bodies are FastAPI's {"detail": "..."}; show just the sentence. */
export function errorText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    return (JSON.parse(raw) as { detail?: string }).detail || raw;
  } catch {
    return raw;
  }
}
