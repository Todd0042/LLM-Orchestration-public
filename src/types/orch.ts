export type ChatSource = "agy" | "claude" | "orchestration";

export type EffortLevel = "low" | "medium" | "high" | "xhigh" | "max";
export type AgyMode = "accept-edits" | "auto" | "plan";

export type TaskKey =
  | "coding"
  | "reasoning"
  | "story"
  | "research"
  | "peer_relay"
  | "image"
  | "video"
  | "music";

export interface ModelInfo {
  id: string;
  nodeId: string;
  kind: "llm" | "image" | "video" | "music";
  displayName: string;
  strengths: string[];
  fit: string;
  contextLength: number;
  status: string;
}

export interface ChatSummary {
  id: string;
  source: ChatSource;
  nodeId: string;
  title: string;
  preview?: string;
  mode?: "solo" | "relay";
  taskType?: TaskKey;
  workspace?: string;
  updatedAt: string;
  pinned?: boolean;
  archived?: boolean;
}

export interface ChatEvent {
  type: "thinking" | "diff" | "tool" | "media";
  tool?: string;
  path?: string;
  oldText?: string;
  newText?: string;
  summary?: string;
  detail?: string;
  text?: string;
  kind?: "image" | "video" | "audio";
  filename?: string;
  nodeId?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  senderLabel: string;
  nodeId: string;
  modelId?: string;
  content: string;
  events: ChatEvent[];
  createdAt: string;
}

export interface ArtifactItem {
  id: string;
  kind: "doc" | "diff" | "image" | "video" | "audio" | "file" | "html";
  title: string;
  path: string;
  nodeId: string;
  mimeType: string;
  meta: {
    oldText?: string;
    newText?: string;
    origin?: string;
    prompt?: string;
    seed?: number;
  };
}

export interface ChatDetail {
  id: string;
  source: ChatSource;
  nodeId: string;
  mode?: "solo" | "relay";
  taskType?: TaskKey;
  title: string;
  messages: ChatMessage[];
  artifacts: ArtifactItem[];
}

export interface TaskDefaultConfig {
  label: string;
  nodeId: string;
  modelId: string;
  engineModel?: string;
  effort?: EffortLevel;
  mode?: AgyMode;
  temperature: number;
  maxTokens: number;
  systemPrompt: string;
}

export interface PeerNodeConfig {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  online?: boolean;
  discovered?: string;
}

export interface OrchestratorEngineConfig {
  nodeId: string;
  modelId: string;
  engineModel?: string;
  effort?: EffortLevel;
  mode?: AgyMode;
  temperature: number;
  maxTokens: number;
}

export interface AgyEngineConfig {
  model: string;
  effort: EffortLevel;
  mode: AgyMode;
  dangerouslySkipPermissions: boolean;
}

export interface ClaudeEngineConfig {
  model: string;
  fallbackModel: string;
  effort: EffortLevel;
  dangerouslySkipPermissions: boolean;
}

export interface LocalEngineConfig {
  enableThinking: boolean;
  temperature: number;
  maxTokens: number;
  systemPrompt: string;
}

export interface AppSettings {
  node: {
    id: string;
    displayName: string;
    role?: "hub" | "worker";
    port: number;
    mdnsEnabled: boolean;
    preferDesktopGpu: boolean;
    hubUrl?: string;
  };
  peers: PeerNodeConfig[];
  paths: {
    agyDir: string;
    claudeDir: string;
    toolsDir: string;
    githubDir?: string;
    vaultDir?: string;
    llmBaseUrl: string;
    comfyUrl: string;
    gpuSchedulerUrl: string;
    useGpuScheduler: boolean;
  };
  auth?: {
    totpStateDir: string;
    trustCloudflareHeaders: boolean;
    sessionIdleSec: number;
    requireAuthEverywhere: boolean;
  };
  engines?: {
    orchestrator?: OrchestratorEngineConfig;
    agy: AgyEngineConfig;
    claude: ClaudeEngineConfig;
    local: LocalEngineConfig;
  };
  terminal: {
    shell: string;
    askApprovalBeforeRun: boolean;
    sudoTimeoutSec: number;
    trustedReadOnlyAutoRun: boolean;
  };
  taskDefaults: Record<TaskKey, TaskDefaultConfig>;
  modelOverrides: Record<
    string,
    { displayName?: string; strengths?: string[]; fit?: string; contextLength?: number }
  >;
  disabledTools: string[];
  autoRouteByTask: boolean;
}

export interface ToolInfo {
  name: string;
  category: string;
  description: string;
  gpuNeed: string;
  enabled: boolean;
  path: string;
}

export interface PasswordPromptEvent {
  type: "password_prompt";
  requestId: string;
  runId: string;
  nodeId: string;
  command: string;
  prompt: string;
  timeoutSec: number;
}

export interface AuthStatus {
  authenticated: boolean;
  authRequired: boolean;
  hasSecret?: boolean;
  remainingSec?: number;
  zone?: "local" | "lan" | "tunnel";
  passkeyCapable?: boolean;
  hasPasskeys?: boolean;
  sessionId?: string;
  method?: string;
}

export interface EngineCatalog {
  agyModels: Array<{ id: string; label: string }>;
  claudePresets: Array<{ id: string; label: string }>;
  efforts: EffortLevel[];
  agyModes: AgyMode[];
}

export interface RepoInventoryRow {
  nodeId: string;
  repoName: string;
  path: string;
  branch: string;
  headSha: string;
  upstreamRemote: string;
  ahead: number;
  behind: number;
  dirtyCount: number;
  untrackedCount: number;
  branches: string[];
  dirtyFiles: string[];
  lastCommitAt: string;
  scannedAt: number;
}

export interface RepoComparisonRow {
  repoName: string;
  status: "in_sync" | "behind_remote" | "diverged" | "dirty" | "branch_mismatch" | "laptop_only" | "desktop_only";
  laptop?: RepoInventoryRow | null;
  desktop?: RepoInventoryRow | null;
}

export interface IntermissionEvent {
  id: number;
  createdAt: number;
  category: string;
  target: string;
  payload: Record<string, unknown>;
  status: "queued" | "reconciling" | "synced" | "conflict_needs_agent";
  resultNote: string;
  syncedAt?: number | null;
}

export interface ToolInventoryRow {
  nodeId: string;
  toolId: string;
  kind: string;
  name: string;
  status: string;
  metadata: Record<string, unknown>;
  updatedAt: number;
}

export interface VaultFileItem {
  relPath: string;
  absPath: string;
  size: number;
  modifiedMs: number;
  mimeType: string;
}

export interface CatchupStatusEvent {
  type: "catchup_status";
  running: boolean;
  total: number;
  done: number;
  conflicts?: number;
  message: string;
}
