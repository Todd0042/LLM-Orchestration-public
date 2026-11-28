import {
  cancelJob,
  createNativeChat,
  fetchApprovals,
  fetchAuthStatus,
  fetchChatDetail,
  fetchChats,
  fetchEngineCatalog,
  fetchModels,
  fetchSettings,
  fetchStatus,
  fetchTools,
  runTerminalScript,
  saveSettingsApi,
  sendChatTurn,
  startPeerRelay,
  updateChat,
  type ApprovalRequest,
} from "@/lib/api";
import type { AppSettings, ArtifactItem, ChatSummary, PasswordPromptEvent } from "@/types/orch";
import type { OrchEvent } from "@/lib/useEventStream";
import { clearLive, getState, setState, updateLive, type Panel, type View } from "./store";

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function toast(text: string, tone: "info" | "error" = "info"): void {
  setState({ toast: { text, tone } });
  window.setTimeout(() => {
    if (getState().toast?.text === text) setState({ toast: null });
  }, tone === "error" ? 9000 : 4000);
}

// ---- bootstrap ------------------------------------------------------------------------------------

export async function loadAuth(): Promise<boolean> {
  const auth = await fetchAuthStatus();
  setState({ auth });
  return !(auth.authRequired && !auth.authenticated);
}

export async function bootstrap(): Promise<void> {
  if (!(await loadAuth())) return;
  const [s, m, eng, c, t] = await Promise.all([
    fetchSettings(),
    fetchModels(),
    fetchEngineCatalog().catch(() => null),
    fetchChats(),
    fetchTools().catch(() => []),
  ]);
  const orch = s.settings.engines?.orchestrator;
  setState({
    settings: s.settings,
    peersStatus: s.peersStatus,
    models: m.models,
    engineCatalog: eng,
    tools: t,
    chats: c.chats,
    modelId: orch?.modelId || s.settings.taskDefaults.coding.modelId,
    nodeId: orch?.nodeId && orch.nodeId !== "auto" ? orch.nodeId : s.settings.node.id,
    engineModel: orch?.engineModel || "",
    effort: orch?.effort || "high",
    agyMode: orch?.mode || "accept-edits",
  });
  const last = localStorage.getItem("orch.mainChat");
  const pick = c.chats.find((x) => x.source === "orchestration" && x.id === last) ?? c.chats.find((x) => x.source === "orchestration");
  if (pick) await openMainChat(pick);
  void refreshStatus();
  void fetchApprovals().then((approvals) => setState({ approvals })).catch(() => null);
}

export async function refreshStatus(): Promise<void> {
  try {
    setState({ status: await fetchStatus() });
  } catch {
    // transient; the strip keeps the last known values
  }
}

export async function refreshChats(): Promise<void> {
  try {
    setState({ chats: (await fetchChats()).chats });
  } catch (e) {
    toast(`Couldn't load chats: ${errText(e)}`, "error");
  }
}

// ---- navigation -----------------------------------------------------------------------------------

export function go(view: View): void {
  setState({ view, sidebarOpen: false });
}

export function openPanel(panel: Panel): void {
  setState({ panel, sidebarOpen: false });
}

export async function openMainChat(c: ChatSummary): Promise<void> {
  const detail = await fetchChatDetail(c.nodeId, "orchestration", c.id);
  localStorage.setItem("orch.mainChat", c.id);
  setState({ mainChat: detail, view: "chat", sidebarOpen: false });
}

export async function openSession(c: ChatSummary): Promise<void> {
  if (c.source === "orchestration") return openMainChat(c);
  try {
    const detail = await fetchChatDetail(c.nodeId, c.source, c.id);
    setState({ session: detail, inspector: "session", artifact: detail.artifacts[0] ?? getState().artifact, sidebarOpen: false });
  } catch (e) {
    toast(`Couldn't open that session: ${errText(e)}`, "error");
  }
}

export function closeSession(): void {
  setState({ session: null });
}

export function openArtifact(a: Partial<ArtifactItem> & { path: string; nodeId: string }): void {
  const full: ArtifactItem = {
    id: a.id || `adhoc_${a.path}`,
    kind: a.kind || "file",
    title: a.title || a.path.split("/").pop() || "file",
    path: a.path,
    nodeId: a.nodeId,
    mimeType: a.mimeType || "",
    meta: a.meta || {},
  };
  setState({ artifact: full, inspector: "artifact" });
}

// ---- conversations --------------------------------------------------------------------------------

export async function newMainChat(title = "New conversation", mode: "solo" | "relay" = "solo"): Promise<void> {
  const created = await createNativeChat({ title, mode, taskType: getState().taskType });
  await refreshChats();
  await openMainChat(created);
}

export async function renameChat(chatId: string, title: string): Promise<void> {
  await updateChat(chatId, { title });
  await refreshChats();
  const m = getState().mainChat;
  if (m?.id === chatId) setState({ mainChat: { ...m, title } });
}

export async function setPinned(chatId: string, pinned: boolean): Promise<void> {
  await updateChat(chatId, { pinned });
  await refreshChats();
}

export async function archiveChat(chatId: string): Promise<void> {
  await updateChat(chatId, { archived: true });
  if (getState().mainChat?.id === chatId) {
    localStorage.removeItem("orch.mainChat");
    setState({ mainChat: null });
  }
  await refreshChats();
  toast("Conversation archived");
}

async function reloadChat(chatId: string): Promise<void> {
  const { mainChat, session } = getState();
  if (mainChat?.id === chatId) setState({ mainChat: await fetchChatDetail(mainChat.nodeId, "orchestration", chatId) });
  if (session?.id === chatId) setState({ session: await fetchChatDetail(session.nodeId, session.source, chatId) });
}

export async function sendMain(content: string): Promise<void> {
  let chat = getState().mainChat;
  if (!chat) {
    await newMainChat(content.slice(0, 60) || "New conversation");
    chat = getState().mainChat;
    if (!chat) return;
  }
  const s = getState();
  updateLive(chat.id, () => ({ steps: [{ step: "Sending…", detail: "" }], streamText: "" }));
  try {
    const res = await sendChatTurn({
      chatNode: chat.nodeId, source: "orchestration", chatId: chat.id, content, taskType: s.taskType,
      nodeId: s.nodeId, modelId: s.modelId, engineModel: s.engineModel || undefined,
      effort: s.effort || undefined, mode: s.agyMode || undefined,
    });
    if (res.jobId) updateLive(chat.id, () => ({ jobId: res.jobId! }));
    await reloadChat(chat.id);
  } catch (e) {
    clearLive(chat.id);
    toast(errText(e), "error");
  }
}

export async function sendSession(content: string): Promise<void> {
  const chat = getState().session;
  if (!chat) return;
  const s = getState();
  updateLive(chat.id, () => ({ steps: [{ step: `Sending to ${chat.source === "agy" ? "AGY" : "Claude"}…`, detail: "" }], streamText: "" }));
  try {
    const res = await sendChatTurn({ chatNode: chat.nodeId, source: chat.source, chatId: chat.id, content, taskType: s.taskType,
                                     nodeId: chat.nodeId, modelId: chat.source === "agy" ? "agy-cli" : "claude-cli" });
    if (res.jobId) updateLive(chat.id, () => ({ jobId: res.jobId! }));
  } catch (e) {
    clearLive(chat.id);
    toast(errText(e), "error");
  }
}

export async function cancelTurn(chatId: string): Promise<void> {
  const job = getState().live[chatId]?.jobId;
  if (job) await cancelJob(job).catch(() => null);
  clearLive(chatId);
  await reloadChat(chatId);
  toast("Stopped");
}

export async function runScript(command: string, nodeId: string): Promise<void> {
  const chat = getState().mainChat;
  try {
    const res = await runTerminalScript({ command, nodeId, chatId: chat?.id ?? "" });
    if (chat) await reloadChat(chat.id);
    if (res.exitCode !== 0) toast(`Command exited with code ${res.exitCode}`, "error");
  } catch (e) {
    toast(errText(e), "error");
  }
}

export async function startRelay(params: Parameters<typeof startPeerRelay>[1]): Promise<void> {
  const chat = getState().mainChat;
  if (!chat) return;
  await startPeerRelay(chat.id, params);
  toast("Relay started; replies will appear here");
}

export async function saveSettings(next: AppSettings): Promise<void> {
  const saved = await saveSettingsApi(next);
  setState({ settings: saved, settingsOpen: false });
  toast("Settings saved");
}

export function setTextScale(scale: number): void {
  localStorage.setItem("orch.textScale", String(scale));
  setState({ textScale: scale });
}

// ---- live events ----------------------------------------------------------------------------------

let statusTimer: number | undefined;
function statusSoon(): void {
  window.clearTimeout(statusTimer);
  statusTimer = window.setTimeout(() => void refreshStatus(), 400);
}

export function handleEvent(raw: OrchEvent): void {
  const ev = raw as Record<string, any>;
  const chatId = String(ev.chatId || "");
  switch (ev.type) {
    case "password_prompt":
      setState((s) => ({ passwordQueue: [...s.passwordQueue, ev as unknown as PasswordPromptEvent] }));
      break;
    case "approval_request":
      setState((s) => (s.approvals.some((a) => a.requestId === ev.requestId) ? {} : { approvals: [...s.approvals, ev as unknown as ApprovalRequest] }));
      statusSoon();
      break;
    case "approval_resolved":
      setState((s) => ({ approvals: s.approvals.filter((a) => a.requestId !== ev.requestId) }));
      statusSoon();
      break;
    case "turn_started":
      updateLive(chatId, () => ({ jobId: String(ev.jobId) }));
      statusSoon();
      break;
    case "orch_progress":
      if (ev.done) clearLive(chatId);
      else updateLive(chatId, (t) => ({ steps: [...t.steps.slice(-14), { step: String(ev.step || "Working…"), detail: String(ev.detail || "") }] }));
      break;
    case "agent_delta":
      updateLive(chatId, (t) => ({ streamText: (t.streamText + String(ev.text || "")).slice(-20000) }));
      break;
    case "chat_updated":
      void reloadChat(chatId).catch(() => null);
      void refreshChats();
      break;
    case "orch_card":
      setState((s) => ({ orchTick: s.orchTick + 1 }));
      return;
    case "chats_changed":
    case "replicated":
      void refreshChats();
      break;
    case "job_done": {
      const s = getState();
      const owner = Object.entries(s.live).find(([, t]) => t.jobId === ev.jobId)?.[0];
      if (owner) {
        clearLive(owner);
        void reloadChat(owner).catch(() => null);
        if (!ev.ok && ev.error && ev.error !== "cancelled") toast(String(ev.error), "error");
      }
      statusSoon();
      break;
    }
    case "chat_error":
      clearLive(chatId);
      toast(String(ev.error || "Something went wrong"), "error");
      break;
    case "job_started":
    case "repo_handoff":
    case "repo_holds_updated":
    case "catchup_status":
    case "alert":
      statusSoon();
      if (ev.type === "alert") toast(`${ev.title}: ${ev.body}`, ev.priority === "high" || ev.priority === "urgent" ? "error" : "info");
      if (ev.type === "repo_handoff" && ev.status && ev.status !== "handed_off" && ev.status !== "already_in_sync") toast(`${ev.repo}: ${ev.message || ev.status}`, "error");
      break;
    default:
      break;
  }
}

export function setConnected(connected: boolean): void {
  if (getState().connected !== connected) setState({ connected });
}

export function setModel(modelId: string, nodeId: string): void {
  setState({ modelId, nodeId, engineModel: "" });
}

export function setMainPanelFromView(view: View): Panel {
  return view === "repos" ? "repos" : view === "sites" ? "sites" : view === "more" ? "more" : null;
}
