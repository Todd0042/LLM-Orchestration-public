import type {
  AppSettings,
  AuthStatus,
  ChatDetail,
  ChatSource,
  ChatSummary,
  EngineCatalog,
  IntermissionEvent,
  ModelInfo,
  PeerNodeConfig,
  RepoComparisonRow,
  RepoInventoryRow,
  TaskKey,
  ToolInfo,
  ToolInventoryRow,
  VaultFileItem,
} from "@/types/orch";

const isTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

let cachedBaseUrl: string | null = null;
let cachedNodeToken = "";
let onUnauthorizedCb: (() => void) | null = null;

export function setOnUnauthorized(cb: (() => void) | null): void {
  onUnauthorizedCb = cb;
}


export async function getApiBase(): Promise<string> {
  if (cachedBaseUrl !== null) return cachedBaseUrl;
  if (isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const res = await invoke<{ backend_url: string; fallback_url?: string; node_token?: string }>("get_node_backend_url");
      const primary = res.backend_url.replace(/\/+$/, "");
      const fallback = (res.fallback_url || "http://127.0.0.1:8790").replace(/\/+$/, "");
      cachedNodeToken = (res.node_token || "").trim();
      if (primary !== fallback) {
        try {
          const ac = new AbortController();
          const t = setTimeout(() => ac.abort(), 1800);
          const ping = await fetch(`${primary}/api/health`, { signal: ac.signal });
          clearTimeout(t);
          if (ping.ok) {
            cachedBaseUrl = primary;
            return cachedBaseUrl;
          }
        } catch {
          // Hub unreachable on LAN -> fall back to local node
        }
        cachedBaseUrl = fallback;
        return cachedBaseUrl;
      }
      cachedBaseUrl = primary;
      return cachedBaseUrl;
    } catch {
      cachedBaseUrl = "http://127.0.0.1:8790";
      return cachedBaseUrl;
    }
  }
  cachedBaseUrl = "";
  return "";
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const base = await getApiBase();
  const tokenHeaders: Record<string, string> = cachedNodeToken ? { "X-Node-Token": cachedNodeToken } : {};
  const r = await fetch(`${base}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-Orch-Client": "1", ...tokenHeaders, ...(init?.headers ?? {}) },
  });
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    onUnauthorizedCb?.();
  }
  if (!r.ok) {
    throw new Error((await r.text().catch(() => "")) || `HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  return apiJson("/api/auth/status");
}

export async function loginTotp(code: string): Promise<{ ok: boolean; remainingSec: number }> {
  return apiJson("/api/auth/login", { method: "POST", body: JSON.stringify({ code }) });
}

type Json = Record<string, any>;

export async function passkeyLoginOptions(): Promise<{ challengeId: string; options: Json }> {
  return apiJson("/api/auth/passkey/login/options", { method: "POST" });
}

export async function passkeyLoginVerify(challengeId: string, credential: Json): Promise<void> {
  await apiJson("/api/auth/passkey/login/verify", { method: "POST", body: JSON.stringify({ challengeId, credential }) });
}

export async function passkeyRegisterOptions(): Promise<{ challengeId: string; options: Json }> {
  return apiJson("/api/auth/passkey/register/options", { method: "POST" });
}

export async function passkeyRegisterVerify(challengeId: string, credential: Json, name = ""): Promise<{ id: string; name: string }> {
  return apiJson("/api/auth/passkey/register/verify", { method: "POST", body: JSON.stringify({ challengeId, credential, name }) });
}

export async function pairStart(): Promise<{ url: string; token: string; expiresInSec: number }> {
  return apiJson("/api/auth/pair/start", { method: "POST" });
}

export async function pairRedeem(token: string): Promise<void> {
  await apiJson("/api/auth/pair/redeem", { method: "POST", body: JSON.stringify({ token }) });
}

export interface DevicesInfo {
  passkeys: Array<{ id: string; name: string; createdAt: number; lastUsedAt: number }>;
  sessions: Array<{ id: string; name: string; method: string; createdAt: number; lastSeen: number; expiresAt: number }>;
  currentSessionId: string;
  passkeyCapable: boolean;
}

export async function fetchDevices(): Promise<DevicesInfo> {
  return apiJson("/api/auth/devices");
}

export async function revokeDevice(kind: "passkey" | "session", id: string): Promise<void> {
  await apiJson("/api/auth/devices/revoke", { method: "POST", body: JSON.stringify({ kind, id }) });
}

export async function logoutTotp(): Promise<void> {
  await apiJson("/api/auth/logout", { method: "POST" });
}

export async function fetchSettings(): Promise<{
  settings: AppSettings;
  peersStatus: PeerNodeConfig[];
}> {
  return apiJson("/api/settings");
}

export async function saveSettingsApi(settings: AppSettings): Promise<AppSettings> {
  const r = await apiJson<{ settings: AppSettings }>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
  return r.settings;
}

export async function fetchEngineCatalog(): Promise<EngineCatalog> {
  return apiJson("/api/engines/models");
}

export async function fetchMcpConfig(): Promise<{
  configPath: string;
  bridgeScript: string;
  claudeCliFlag: string;
  agyRegisterCommand: string;
}> {
  return apiJson("/api/mcp/config");
}

export async function fetchModels(): Promise<{ nodeId: string; models: ModelInfo[] }> {
  return apiJson("/api/models");
}

export async function fetchChats(): Promise<{ nodeId: string; chats: ChatSummary[] }> {
  return apiJson("/api/chats");
}

export async function fetchChatDetail(
  nodeId: string,
  source: ChatSource,
  id: string
): Promise<ChatDetail> {
  return apiJson(`/api/chats/${encodeURIComponent(nodeId)}/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
}

export async function updateChat(chatId: string, patch: { title?: string; pinned?: boolean; archived?: boolean }): Promise<void> {
  await apiJson(`/api/chats/native/${encodeURIComponent(chatId)}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export async function fetchStatus(): Promise<import("@/state/store").StatusSummary> {
  return apiJson("/api/status");
}

export async function createNativeChat(params: {
  title: string;
  mode: "solo" | "relay";
  taskType: TaskKey;
  workspace?: string;
}): Promise<ChatSummary> {
  return apiJson("/api/chats/native", { method: "POST", body: JSON.stringify(params) });
}

export async function sendNativeMessage(
  chatId: string,
  body: {
    content: string;
    taskType: TaskKey;
    nodeId: string;
    modelId: string;
    engineModel?: string;
    effort?: string;
    mode?: string;
    fallbackModel?: string;
    runTools?: boolean;
  }
): Promise<{ ok?: boolean; error?: string; jobId?: string }> {
  return apiJson<{ ok?: boolean; error?: string; jobId?: string }>(`/api/chats/native/${encodeURIComponent(chatId)}/send`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function sendChatTurn(params: {
  chatNode: string;
  source: ChatSource;
  chatId: string;
  content: string;
  taskType: TaskKey;
  nodeId: string;
  modelId: string;
  engineModel?: string;
  effort?: string;
  mode?: string;
  fallbackModel?: string;
}): Promise<{ ok?: boolean; error?: string; jobId?: string }> {
  if (params.source === "orchestration") {
    return sendNativeMessage(params.chatId, {
      content: params.content,
      taskType: params.taskType,
      nodeId: params.nodeId,
      modelId: params.modelId,
      engineModel: params.engineModel,
      effort: params.effort,
      mode: params.mode,
      fallbackModel: params.fallbackModel,
      runTools: true,
    });
  }
  return apiJson<{ ok?: boolean; error?: string; jobId?: string }>(
    `/api/chats/${encodeURIComponent(params.chatNode)}/${encodeURIComponent(params.source)}/${encodeURIComponent(params.chatId)}/send`,
    {
      method: "POST",
      body: JSON.stringify({
        content: params.content,
        taskType: params.taskType,
        nodeId: params.nodeId,
        modelId: params.modelId,
        engineModel: params.engineModel,
        effort: params.effort,
        mode: params.mode,
        fallbackModel: params.fallbackModel,
      }),
    }
  );
}

export async function startPeerRelay(
  chatId: string,
  body: {
    topic: string;
    turns: number;
    nodeA: string;
    modelA: string;
    roleA: string;
    nodeB: string;
    modelB: string;
    roleB: string;
  }
): Promise<void> {
  await apiJson(`/api/chats/native/${encodeURIComponent(chatId)}/relay`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function runTerminalScript(params: {
  command: string;
  cwd?: string;
  nodeId?: string;
  chatId?: string;
}): Promise<{ runId: string; exitCode: number; output: string }> {
  return apiJson("/api/terminal/run", { method: "POST", body: JSON.stringify(params) });
}

export async function submitSudoPassword(params: {
  requestId: string;
  nodeId: string;
  password: string | null;
}): Promise<void> {
  await apiJson("/api/terminal/password", { method: "POST", body: JSON.stringify(params) });
}

export async function fetchTools(): Promise<ToolInfo[]> {
  const res = await apiJson<{ tools: ToolInfo[] }>("/api/tools");
  return res.tools;
}

export async function fetchRepoInventory(refresh = false): Promise<{
  nodeId: string;
  desktopOnline: boolean;
  localRepos: RepoInventoryRow[];
  comparison: RepoComparisonRow[];
  queue: IntermissionEvent[];
}> {
  return apiJson(`/api/repos/inventory?refresh=${refresh ? "true" : "false"}`);
}

export async function syncRepoAction(params: {
  repoName: string;
  action: "pull" | "reconcile";
  targetNode?: "both" | "laptop" | "desktop";
  agent?: "agy-cli" | "claude-cli";
  chatId?: string;
}): Promise<Record<string, unknown>> {
  return apiJson("/api/repos/sync", { method: "POST", body: JSON.stringify(params) });
}

export async function triggerDesktopCatchup(): Promise<{ processed: number; synced: number; conflicts: number }> {
  return apiJson("/api/sync/catchup", { method: "POST" });
}

export async function fetchToolInventory(): Promise<{ nodeId: string; items: ToolInventoryRow[] }> {
  return apiJson("/api/tools/inventory");
}

export async function fetchVaultList(): Promise<{ vaultDir: string; files: VaultFileItem[] }> {
  return apiJson("/api/vault/list");
}

export async function readVaultFile(relPath: string): Promise<{ relPath: string; path: string; content: string }> {
  return apiJson(`/api/vault/read?relPath=${encodeURIComponent(relPath)}`);
}

export async function writeVaultFile(params: {
  relPath: string;
  content?: string;
  contentBase64?: string;
}): Promise<{ ok: boolean; relPath: string; path: string }> {
  return apiJson("/api/vault/write", { method: "POST", body: JSON.stringify(params) });
}


export async function fetchFileContent(
  path: string,
  fileNode = "local"
): Promise<{ path: string; nodeId: string; content: string }> {
  return apiJson(
    `/api/file?path=${encodeURIComponent(path)}&fileNode=${encodeURIComponent(fileNode)}`
  );
}

export async function buildMediaUrl(path: string, fileNode = "local"): Promise<string> {
  const base = await getApiBase();
  const signed = await apiJson<{ query: string }>(
    `/api/media/sign?path=${encodeURIComponent(path)}&fileNode=${encodeURIComponent(fileNode)}`
  );
  return `${base}/api/media?${signed.query}`;
}

/** WebSocket URL for `path` with a one-time ticket (the node token never goes in a URL). */
export async function ticketedWsUrl(path: string): Promise<string> {
  const base = await getApiBase();
  const wsBase = base
    ? base.replace(/^http/, "ws")
    : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const { ticket } = await apiJson<{ ticket: string }>("/api/ws/ticket", { method: "POST" });
  return `${wsBase}${path}?ticket=${encodeURIComponent(ticket)}`;
}

export function eventStreamUrl(): Promise<string> {
  return ticketedWsUrl("/ws/events");
}

// ---- Background jobs -------------------------------------------------------------------------

export interface JobInfo {
  id: string;
  kind: string;
  title: string;
  status: "running" | "done" | "error" | "cancelled";
  progressText: string;
  nodeId: string;
  error: string;
  result: Record<string, unknown>;
  createdAt: number;
  updatedAt: number;
}

export async function fetchJob(jobId: string): Promise<JobInfo> {
  return apiJson(`/api/jobs/${encodeURIComponent(jobId)}`);
}

/** Resolve when a background job finishes (polls; live progress arrives over the event stream). */
export async function waitForJob(jobId: string, timeoutMs = 4 * 3600 * 1000): Promise<JobInfo> {
  const started = Date.now();
  let delay = 800;
  for (;;) {
    const j = await fetchJob(jobId).catch(() => null);
    if (j && j.status !== "running") return j;
    if (Date.now() - started > timeoutMs) throw new Error("Timed out waiting for the job");
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 1.4, 4000);
  }
}

export async function cancelJob(jobId: string): Promise<void> {
  await apiJson(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

// ---- Approvals -------------------------------------------------------------------------------

export interface ApprovalRequest {
  requestId: string;
  kind: string;
  nodeId: string;
  command: string;
  cwd: string;
  chatId: string;
  createdAt: number;
  timeoutSec: number;
}

export async function fetchApprovals(): Promise<ApprovalRequest[]> {
  return (await apiJson<{ pending: ApprovalRequest[] }>("/api/approvals")).pending;
}

export async function decideApproval(requestId: string, approve: boolean): Promise<void> {
  await apiJson(`/api/approvals/${encodeURIComponent(requestId)}`, { method: "POST", body: JSON.stringify({ approve }) });
}

// ---- Repo sync board -------------------------------------------------------------------------

export interface RepoNodeState {
  branch: string;
  headSha: string;
  ahead: number;
  behind: number;
  dirtyCount: number;
  untrackedCount: number;
  lastCommitAt: string;
}

export interface RepoRecommendation {
  action: "none" | "handoff" | "gitignore" | "review";
  text: string;
  from?: string;
  to?: string;
  node?: string;
}

export interface RepoBoardRow {
  repo: string;
  status: "in_sync" | "dirty" | "branch_mismatch" | "different_commits" | "single_node";
  nodes: Record<string, RepoNodeState>;
  dirtyOn: string[];
  active: { nodeId: string; source: string; lastSeen: number } | null;
  recommendation: RepoRecommendation;
}

export interface HandoffRecord {
  id: string;
  repo: string;
  from: string;
  to: string;
  status: string;
  wipRef: string;
  detail: { message?: string; actions?: string[] };
  createdAt: number;
}

export async function fetchRepoBoard(refresh = false): Promise<{ nodeId: string; peersOnline: Record<string, boolean>; repos: RepoBoardRow[]; handoffs: HandoffRecord[] }> {
  return apiJson(`/api/repos/board?refresh=${refresh ? "true" : "false"}`);
}

export async function startHandoff(repo: string, toNode: string): Promise<{ jobId: string }> {
  return apiJson("/api/repos/handoff", { method: "POST", body: JSON.stringify({ repo, toNode }) });
}

export interface RepoTrackerNodeState {
  branch: string;
  headSha: string;
  remoteHeadSha: string;
  ahead: number;
  behind: number;
  dirtyCount: number;
  untrackedCount: number;
  stashCount: number;
  worktreeCount: number;
  detached: boolean;
  lastCommitAt: number;
  lastChangeAt: number;
  activeProcess: string;
  diffStat: string;
  scannedAt: number;
}

export interface RepoTrackerHold {
  state: "active" | "clear" | "needs_answer";
  setBy: string;
  setAt: number;
  note: string;
}

export interface RepoTrackerRow {
  repoKey: string;
  repoName: string;
  threeWayStatus: "in_sync" | "dirty" | "branch_mismatch" | "diverged" | "single_node";
  laptop: RepoTrackerNodeState | null;
  desktop: RepoTrackerNodeState | null;
  github: { branch: string; headSha: string; branchCount: number } | null;
  branches: Record<string, Record<string, string>>;
  remoteUrl: string;
  activeProcess: string;
  lastChangeAt: number;
  hold: RepoTrackerHold | null;
}

export async function fetchRepoTracker(refresh = false): Promise<{
  nodeId: string;
  peersOnline: Record<string, boolean>;
  repos: RepoTrackerRow[];
  counts: { active: number; needsAnswer: number };
}> {
  return apiJson(`/api/repos/tracker?refresh=${refresh ? "true" : "false"}`);
}

export async function setRepoHold(
  repoName: string,
  state: "active" | "clear" | "needs_answer",
  note = ""
): Promise<{ ok: boolean; hold: RepoTrackerHold; counts: { active: number; needsAnswer: number } }> {
  return apiJson("/api/repos/holds", {
    method: "POST",
    body: JSON.stringify({ repoName, state, note }),
  });
}

// ---- Services, backups, failover, audit, nodes ------------------------------------------------

export interface ServiceStatus {
  id: string;
  name: string;
  publicHost: string;
  status: "healthy" | "failing" | "restarting" | "gave_up" | "unknown";
  detail: string;
  consecutiveFailures: number;
  restartsLastHour: number;
  failover: string;
  backups: number;
}

export async function fetchServices(): Promise<{
  nodes: Record<string, ServiceStatus[] | null>;
  failover: { state: string; enabled?: boolean; role?: string; primaryNode?: string; standbyNode?: string };
  backups: { at?: number; target?: string; results?: Record<string, string> };
}> {
  return apiJson("/api/services");
}

export async function fetchServiceEvents(): Promise<Array<{ id: number; ts: number; node_id: string; service_id: string; kind: string; detail: string }>> {
  return (await apiJson<{ events: Array<{ id: number; ts: number; node_id: string; service_id: string; kind: string; detail: string }> }>("/api/services/events")).events;
}

export async function runBackupsNow(): Promise<Record<string, unknown>> {
  return apiJson("/api/backups/run", { method: "POST" });
}

export async function sendTestAlert(): Promise<{ sentToPhone: boolean }> {
  return apiJson("/api/alerts/test", { method: "POST" });
}

export interface AuditEntry {
  id: number;
  ts: number;
  nodeId: string;
  zone: string;
  client: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

export async function fetchAudit(limit = 200): Promise<AuditEntry[]> {
  return (await apiJson<{ entries: AuditEntry[] }>(`/api/audit?limit=${limit}`)).entries;
}

export async function fetchNodes(): Promise<{ local: Record<string, unknown>; hub: string; peers: Array<PeerNodeConfig & { activeUrl?: string; profile?: Record<string, unknown> }> }> {
  return apiJson("/api/nodes");
}

/** Plugins whose server side loaded on this node (see server/plugins). */
export async function fetchFeatures(): Promise<{ features: Array<{ id: string; label: string }> }> {
  return apiJson("/api/features");
}
