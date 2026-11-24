import { useSyncExternalStore } from "react";
import type {
  AgyMode,
  AppSettings,
  ArtifactItem,
  AuthStatus,
  ChatDetail,
  ChatSummary,
  EffortLevel,
  EngineCatalog,
  ModelInfo,
  PasswordPromptEvent,
  PeerNodeConfig,
  TaskKey,
  ToolInfo,
} from "@/types/orch";
import type { ApprovalRequest } from "@/lib/api";

/** Where the phone layout is. Wide screens show chat + inspector together and use `panel` for the rest. */
export type View = "chat" | "sessions" | "repos" | "sites" | "more";
export type Panel = null | "repos" | "sites" | "activity" | "more";

export interface StatusSummary {
  nodeId: string;
  role: string;
  nodes: Array<{ id: string; online: boolean; role: string; self: boolean }>;
  jobsRunning: Array<{ id: string; kind: string; title: string; progress: string }>;
  approvals: number;
  repoConflicts24h: number;
  repoHoldsCount?: number;
  repoNeedsAnswerCount?: number;
  sitesDown: string[];
  failover: string;
  agentsOut?: Record<string, { name: string; until: number; reason: string }>;
}

export interface LiveTurn {
  jobId: string;
  steps: Array<{ step: string; detail: string }>;
  streamText: string;
}

export interface AppState {
  auth: AuthStatus | null;
  settings: AppSettings | null;
  peersStatus: PeerNodeConfig[];
  models: ModelInfo[];
  engineCatalog: EngineCatalog | null;
  tools: ToolInfo[];
  status: StatusSummary | null;
  connected: boolean;

  chats: ChatSummary[];
  mainChat: ChatDetail | null;
  session: ChatDetail | null;
  artifact: ArtifactItem | null;
  inspector: "session" | "artifact";

  view: View;
  panel: Panel;
  sidebarOpen: boolean;
  settingsOpen: boolean;
  locksOpen: boolean;

  taskType: TaskKey;
  modelId: string;
  nodeId: string;
  engineModel: string;
  effort: EffortLevel | "";
  agyMode: AgyMode | "";

  live: Record<string, LiveTurn>;
  approvals: ApprovalRequest[];
  passwordQueue: PasswordPromptEvent[];
  toast: { text: string; tone: "info" | "error" } | null;
  textScale: number;
}

const initial: AppState = {
  auth: null,
  settings: null,
  peersStatus: [],
  models: [],
  engineCatalog: null,
  tools: [],
  status: null,
  connected: false,
  chats: [],
  mainChat: null,
  session: null,
  artifact: null,
  inspector: "session",
  view: "chat",
  panel: null,
  sidebarOpen: false,
  settingsOpen: false,
  locksOpen: false,
  taskType: "coding",
  modelId: "",
  nodeId: "auto",
  engineModel: "",
  effort: "high",
  agyMode: "accept-edits",
  live: {},
  approvals: [],
  passwordQueue: [],
  toast: null,
  textScale: Number(localStorage.getItem("orch.textScale") || "1") || 1,
};

type Listener = () => void;
let state: AppState = initial;
const listeners = new Set<Listener>();

export function getState(): AppState {
  return state;
}

export function setState(patch: Partial<AppState> | ((s: AppState) => Partial<AppState>)): void {
  const next = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Select one slice. Selectors must return existing references (no new objects/arrays). */
export function useApp<T>(selector: (s: AppState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(state));
}

export function updateLive(chatId: string, fn: (t: LiveTurn) => Partial<LiveTurn>): void {
  setState((s) => {
    const cur = s.live[chatId] ?? { jobId: "", steps: [], streamText: "" };
    return { live: { ...s.live, [chatId]: { ...cur, ...fn(cur) } } };
  });
}

export function clearLive(chatId: string): void {
  setState((s) => {
    const next = { ...s.live };
    delete next[chatId];
    return { live: next };
  });
}
