import { useEffect, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import type {
  AgyMode,
  AppSettings,
  EffortLevel,
  EngineCatalog,
  ModelInfo,
  PeerNodeConfig,
  TaskKey,
  ToolInfo,
} from "@/types/orch";
import { fetchEngineCatalog, fetchMcpConfig } from "@/lib/api";
import { buildOrchestratorOptions, sortAgyModels, sortModelsAlpha } from "@/lib/modelSort";
import { Dropdown } from "./ui/Dropdown";

interface SettingsModalProps {
  open: boolean;
  settings: AppSettings;
  peersStatus: PeerNodeConfig[];
  models: ModelInfo[];
  tools: ToolInfo[];
  onSave: (next: AppSettings) => Promise<void>;
  onClose: () => void;
}

const EFFORTS: EffortLevel[] = ["low", "medium", "high", "xhigh", "max"];
const MODES: AgyMode[] = ["accept-edits", "auto", "plan"];

export function SettingsModal({
  open,
  settings,
  peersStatus,
  models,
  tools,
  onSave,
  onClose,
}: SettingsModalProps) {
  const [draft, setDraft] = useState<AppSettings>(settings);
  const [tab, setTab] = useState<"engines" | "tasks" | "models" | "peers" | "gpu" | "tools">("engines");
  const [newPeerName, setNewPeerName] = useState("");
  const [newPeerUrl, setNewPeerUrl] = useState("http://192.168.1.");
  const [catalog, setCatalog] = useState<EngineCatalog | null>(null);
  const [mcpInfo, setMcpInfo] = useState<{ agyRegisterCommand: string; claudeCliFlag: string } | null>(null);

  useEffect(() => {
    if (open) {
      setDraft(settings);
      void fetchEngineCatalog().then(setCatalog).catch(() => null);
      void fetchMcpConfig().then(setMcpInfo).catch(() => null);
    }
  }, [open, settings]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const taskKeys = Object.keys(draft.taskDefaults) as TaskKey[];
  const orch = draft.engines?.orchestrator ?? {
    nodeId: draft.node.id || "laptop",
    modelId: "qwen3-8b",
    engineModel: "",
    effort: "high" as EffortLevel,
    mode: "accept-edits" as AgyMode,
    temperature: 0.25,
    maxTokens: 2560,
  };
  const agy = draft.engines?.agy ?? {
    model: "gemini-4-neon-high",
    effort: "high" as EffortLevel,
    mode: "accept-edits" as AgyMode,
    dangerouslySkipPermissions: false,
  };
  const claude = draft.engines?.claude ?? {
    model: "opus",
    fallbackModel: "sonnet",
    effort: "high" as EffortLevel,
    dangerouslySkipPermissions: false,
  };
  const localCfg = draft.engines?.local ?? {
    enableThinking: true,
    temperature: 0.2,
    maxTokens: 2048,
    systemPrompt: "",
  };

  const setEngines = (patch: Partial<NonNullable<AppSettings["engines"]>>) =>
    setDraft({
      ...draft,
      engines: { orchestrator: orch, agy, claude, local: localCfg, ...patch },
    });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-xs p-2 lg:p-4" onClick={onClose}>
      <div
        className="w-full max-w-[calc(100vw-16px)] max-h-[calc(100dvh-16px)] lg:h-auto lg:max-w-4xl lg:max-h-[88dvh] flex flex-col rounded-xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-strong)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-2.5 border-b flex flex-wrap items-center justify-between gap-2 shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <h3 className="text-base lg:text-sm font-semibold min-w-0 truncate">LLM-Orchestration Settings</h3>
          <button
            onClick={onClose}
            title="Close Settings (Esc)"
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-xs font-medium hover:opacity-90 cursor-pointer"
            style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
          >
            <X className="w-4 h-4" />
            Close
          </button>
        </div>

        <div className="px-4 pt-2 border-b flex flex-wrap items-center gap-1.5 text-sm lg:text-xs shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          {(
            [
              ["engines", "Orchestrator & Engines"],
              ["tasks", "Specialist Routing"],
              ["models", "Models & Strengths"],
              ["peers", "Hub & LAN Peers"],
              ["gpu", "GPU, Vault & Paths"],
              ["tools", "Sudo & Python Tools"],
            ] as const
          ).map(([k, lbl]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className="px-3 py-1.5 rounded-t-md border-b-2 font-medium cursor-pointer"
              style={{
                borderColor: tab === k ? "var(--accent-primary)" : "transparent",
                color: tab === k ? "var(--accent-primary)" : "var(--text-secondary)",
              }}
            >
              {lbl}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4 text-sm lg:text-xs">
          {tab === "engines" && (
            <div className="space-y-4">
              {/* Primary Orchestrator LLM */}
              <div className="rounded-lg border p-3 space-y-2.5" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--accent-primary)" }}>
                <div className="font-semibold text-sm" style={{ color: "var(--accent-emerald)" }}>
                  Primary Orchestrator LLM (Always handles your main chat)
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                  <Dropdown
                    value={orch.nodeId}
                    onChange={(v) => setEngines({ orchestrator: { ...orch, nodeId: v } })}
                    options={[
                      { value: "desktop", label: "Node: Desktop" },
                      { value: "laptop", label: "Node: Laptop" },
                      { value: "auto", label: "Node: Auto" },
                    ]}
                  />
                  <Dropdown
                    value={`${orch.nodeId === "auto" ? draft.node.id || "laptop" : orch.nodeId}::${orch.modelId}`}
                    onChange={(raw) => {
                      const [nid, mid] = raw.split("::");
                      setEngines({ orchestrator: { ...orch, nodeId: nid, modelId: mid } });
                    }}
                    menuWidthClass="min-w-[17.5rem]"
                    options={buildOrchestratorOptions(models, draft.node.id || "laptop")}
                  />
                  <div className="flex items-center gap-2">
                    <span className="text-[0.6875rem] shrink-0" style={{ color: "var(--text-muted)" }}>
                      Temp:
                    </span>
                    <input
                      type="number"
                      step={0.05}
                      value={orch.temperature}
                      onChange={(e) => setEngines({ orchestrator: { ...orch, temperature: Number(e.target.value) } })}
                      className="w-20 rounded border px-2 py-1 font-mono-code"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                  </div>
                </div>
              </div>

              {/* AGY */}
              <div className="rounded-lg border p-3 space-y-2.5" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div className="font-semibold text-sm" style={{ color: "var(--accent-primary)" }}>
                  AGY CLI Settings
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Model (`agy models`)
                    </label>
                    <Dropdown
                      value={agy.model}
                      onChange={(v) => setEngines({ agy: { ...agy, model: v } })}
                      menuWidthClass="min-w-[16.25rem]"
                      options={sortAgyModels(catalog?.agyModels || [{ id: agy.model, label: agy.model }]).map((m) => ({
                        value: m.id,
                        label: m.label,
                      }))}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Thinking Effort (`--effort`)
                    </label>
                    <Dropdown
                      value={agy.effort}
                      onChange={(v) => setEngines({ agy: { ...agy, effort: v as EffortLevel } })}
                      options={EFFORTS.map((e) => ({ value: e, label: e.toUpperCase() }))}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Execution Mode (`--mode`)
                    </label>
                    <Dropdown
                      value={agy.mode}
                      onChange={(v) => setEngines({ agy: { ...agy, mode: v as AgyMode } })}
                      options={MODES.map((m) => ({ value: m, label: m }))}
                    />
                  </div>
                </div>
                <label className="inline-flex items-center gap-2 font-mono-code text-[0.6875rem]">
                  <input
                    type="checkbox"
                    checked={agy.dangerouslySkipPermissions}
                    onChange={(e) => setEngines({ agy: { ...agy, dangerouslySkipPermissions: e.target.checked } })}
                  />
                  --dangerously-skip-permissions
                </label>
              </div>

              {/* Claude */}
              <div className="rounded-lg border p-3 space-y-2.5" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div className="font-semibold text-sm" style={{ color: "var(--accent-amber)" }}>
                  Claude Code CLI Settings
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Model (`--model`)
                    </label>
                    <input
                      value={claude.model}
                      onChange={(e) => setEngines({ claude: { ...claude, model: e.target.value } })}
                      placeholder="opus / sonnet / haiku"
                      className="w-full rounded border px-2.5 py-1.5 font-mono-code"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Fallback Model (`--fallback-model`)
                    </label>
                    <input
                      value={claude.fallbackModel}
                      onChange={(e) => setEngines({ claude: { ...claude, fallbackModel: e.target.value } })}
                      placeholder="sonnet"
                      className="w-full rounded border px-2.5 py-1.5 font-mono-code"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                      Thinking Effort (`--effort`)
                    </label>
                    <Dropdown
                      value={claude.effort}
                      onChange={(v) => setEngines({ claude: { ...claude, effort: v as EffortLevel } })}
                      options={EFFORTS.map((e) => ({ value: e, label: e.toUpperCase() }))}
                    />
                  </div>
                </div>
                <label className="inline-flex items-center gap-2 font-mono-code text-[0.6875rem]">
                  <input
                    type="checkbox"
                    checked={claude.dangerouslySkipPermissions}
                    onChange={(e) => setEngines({ claude: { ...claude, dangerouslySkipPermissions: e.target.checked } })}
                  />
                  --dangerously-skip-permissions (auto-injected MCP bridge attached)
                </label>
              </div>

              {/* Local LLM + MCP Bridge info */}
              <div className="rounded-lg border p-3 space-y-2" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div className="font-semibold">Local GGUFs &amp; Bidirectional MCP Bridge</div>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 items-center">
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={localCfg.enableThinking}
                      onChange={(e) => setEngines({ local: { ...localCfg, enableThinking: e.target.checked } })}
                    />
                    Allow reasoning / CoT traces
                  </label>
                  <div className="flex items-center gap-2">
                    <span>Temp:</span>
                    <input
                      type="number"
                      step={0.05}
                      value={localCfg.temperature}
                      onChange={(e) => setEngines({ local: { ...localCfg, temperature: Number(e.target.value) } })}
                      className="w-20 rounded border px-2 py-1 font-mono-code"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <span>Max Tokens:</span>
                    <input
                      type="number"
                      step={128}
                      value={localCfg.maxTokens}
                      onChange={(e) => setEngines({ local: { ...localCfg, maxTokens: Number(e.target.value) || 2048 } })}
                      className="w-24 rounded border px-2 py-1 font-mono-code"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                  </div>
                </div>
                {mcpInfo && (
                  <div className="text-[0.6562rem] font-mono-code p-2 rounded border break-all" style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)", color: "var(--text-secondary)" }}>
                    Register AGY MCP: {mcpInfo.agyRegisterCommand}
                  </div>
                )}
              </div>
            </div>
          )}

          {tab === "tasks" && (
            <div className="space-y-3">
              <div className="text-[0.75rem] px-3 py-2 rounded-lg border" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)", color: "var(--text-secondary)" }}>
                Assign which specialist model or CLI agent your <strong>Primary Orchestrator</strong> dispatches behind the scenes for each task (you always talk directly to the Orchestrator).
              </div>

              <div className="space-y-2">
                {taskKeys.map((tk) => {
                  const td = draft.taskDefaults[tk];
                  const isAgy = td.modelId === "agy-cli" || td.modelId === "agy";
                  const isClaude = td.modelId === "claude-cli" || td.modelId === "claude";
                  const isMedia = tk === "image" || tk === "video" || tk === "music";

                  const agyChoices = sortAgyModels(
                    catalog?.agyModels || [
                      { id: "gemini-4-neon-high", label: "Gemini 4 Neon (High)" },
                      { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)" },
                      { id: "claude-opus-5-5-high", label: "Claude Opus 5.5 (High)" },
                      { id: "gemini-3.8-flash-high", label: "Gemini 3.8 Flash (High)" },
                    ]
                  ).map((m) => ({ value: m.id, label: m.label }));

                  const claudeChoices = (catalog?.claudePresets || [
                    { id: "opus", label: "Claude Opus" },
                    { id: "sonnet", label: "Claude Sonnet" },
                    { id: "haiku", label: "Claude Haiku" },
                  ]).map((m) => ({ value: m.id, label: m.label }));

                  const taskModelOptions = isMedia
                    ? sortModelsAlpha(models, tk as "image" | "video" | "music").map((m) => ({
                        value: m.id,
                        label: `${m.displayName} [${m.nodeId}]`,
                        badge: m.strengths[0],
                        hint: `${m.strengths.join(" · ")}`,
                      }))
                    : buildOrchestratorOptions(models, draft.node.id || "laptop", { valueMode: "id" });

                  return (
                    <div
                      key={tk}
                      className="rounded-lg border p-3 grid grid-cols-1 md:grid-cols-[170px_160px_220px_1fr] gap-2 items-center"
                      style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                    >
                      <div>
                        <div className="font-semibold">{td.label}</div>
                        <div className="text-[0.625rem] font-mono-code" style={{ color: "var(--text-muted)" }}>
                          task: {tk}
                        </div>
                      </div>
                      <Dropdown
                        value={td.nodeId}
                        onChange={(v) =>
                          setDraft({
                            ...draft,
                            taskDefaults: { ...draft.taskDefaults, [tk]: { ...td, nodeId: v } },
                          })
                        }
                        options={[
                          { value: "auto", label: "Auto (Best GPU)" },
                          { value: "laptop", label: "Laptop" },
                          { value: "desktop", label: "Desktop" },
                        ]}
                        menuWidthClass="min-w-[10.625rem]"
                      />
                      <Dropdown
                        value={td.modelId}
                        onChange={(v) => {
                          const nextPatch =
                            v === "agy-cli"
                              ? { engineModel: td.engineModel || agy.model, effort: td.effort || agy.effort, mode: td.mode || agy.mode }
                              : v === "claude-cli"
                                ? { engineModel: td.engineModel || claude.model, effort: td.effort || claude.effort }
                                : {};
                          setDraft({
                            ...draft,
                            taskDefaults: { ...draft.taskDefaults, [tk]: { ...td, modelId: v, ...nextPatch } },
                          });
                        }}
                        options={taskModelOptions}
                        menuWidthClass="min-w-[17.5rem]"
                      />

                      {isAgy || isClaude ? (
                        <div className="grid grid-cols-2 gap-1.5 min-w-0">
                          <Dropdown
                            value={td.engineModel || (isAgy ? agy.model : claude.model)}
                            onChange={(v) =>
                              setDraft({
                                ...draft,
                                taskDefaults: { ...draft.taskDefaults, [tk]: { ...td, engineModel: v } },
                              })
                            }
                            options={isAgy ? agyChoices : claudeChoices}
                            menuWidthClass="min-w-[15.0rem]"
                            placeholder="Select model"
                          />
                          <Dropdown
                            value={td.effort || (isAgy ? agy.effort : claude.effort)}
                            onChange={(v) =>
                              setDraft({
                                ...draft,
                                taskDefaults: { ...draft.taskDefaults, [tk]: { ...td, effort: v as EffortLevel } },
                              })
                            }
                            options={EFFORTS.map((e) => ({ value: e, label: `Think: ${e}` }))}
                            menuWidthClass="min-w-[8.75rem]"
                          />
                        </div>
                      ) : isMedia ? (
                        <div className="text-[0.6875rem] font-mono-code px-2 py-1 rounded border" style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)", color: "var(--text-muted)" }}>
                          Exclusive GPU workflow
                        </div>
                      ) : (
                        <div className="flex items-center gap-2">
                          <span className="text-[0.6562rem] shrink-0" style={{ color: "var(--text-muted)" }} title="Sampling temperature: lower (0.1–0.2) is more precise; higher (0.7+) is more creative">
                            Temp:
                          </span>
                          <input
                            type="number"
                            step={0.05}
                            min={0}
                            max={1.5}
                            value={td.temperature}
                            onChange={(e) =>
                              setDraft({
                                ...draft,
                                taskDefaults: {
                                  ...draft.taskDefaults,
                                  [tk]: { ...td, temperature: Number(e.target.value) },
                                },
                              })
                            }
                            className="w-20 rounded border px-2 py-1 font-mono-code"
                            style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                            title="Sampling temperature (0 = deterministic, 0.2 = coding, 0.7+ = creative)"
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {tab === "models" && (
            <div className="space-y-2">
              {models.map((m) => {
                const ov = draft.modelOverrides[m.id] || {};
                return (
                  <div
                    key={`${m.nodeId}:${m.id}`}
                    className="rounded-lg border p-2.5 grid grid-cols-1 md:grid-cols-3 gap-2 items-center"
                    style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                  >
                    <div>
                      <div className="font-semibold">{m.displayName}</div>
                      <div className="text-[0.625rem] font-mono-code" style={{ color: "var(--text-muted)" }}>
                        [{m.nodeId}] {m.id} · {m.status}
                      </div>
                    </div>
                    <input
                      value={ov.strengths ? ov.strengths.join(", ") : m.strengths.join(", ")}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          modelOverrides: {
                            ...draft.modelOverrides,
                            [m.id]: {
                              ...ov,
                              strengths: e.target.value
                                .split(",")
                                .map((s) => s.trim())
                                .filter(Boolean),
                            },
                          },
                        })
                      }
                      placeholder="Strength badges (comma separated)"
                      className="rounded border px-2 py-1 text-xs"
                      style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                    />
                    <div className="text-[0.6875rem]" style={{ color: "var(--text-secondary)" }}>
                      {m.fit} · {m.contextLength.toLocaleString()} ctx
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {tab === "peers" && (
            <div className="space-y-3">
              <div className="rounded-lg border p-3 space-y-2" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div className="font-semibold">This Machine (Hub / Worker Role)</div>
                <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
                  <input
                    value={draft.node.id}
                    onChange={(e) => setDraft({ ...draft, node: { ...draft.node, id: e.target.value } })}
                    placeholder="Node ID (laptop / desktop)"
                    className="rounded border px-2 py-1 font-mono-code"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <input
                    value={draft.node.displayName}
                    onChange={(e) => setDraft({ ...draft, node: { ...draft.node, displayName: e.target.value } })}
                    placeholder="Display Name"
                    className="rounded border px-2 py-1"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <Dropdown
                    value={draft.node.role || "hub"}
                    onChange={(v) => setDraft({ ...draft, node: { ...draft.node, role: v as "hub" | "worker" } })}
                    options={[
                      { value: "hub", label: "Role: Main Hub" },
                      { value: "worker", label: "Role: Worker" },
                    ]}
                  />
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={draft.node.mdnsEnabled}
                      onChange={(e) => setDraft({ ...draft, node: { ...draft.node, mdnsEnabled: e.target.checked } })}
                    />
                    mDNS discovery
                  </label>
                </div>
                <div className="text-[0.6562rem] font-mono-code break-all" style={{ color: "var(--text-muted)" }}>
                  The machine-to-machine token never leaves your computers (it lives in ~/.config/llm-orchestration/node_token). Node, peer, auth and security settings can only be changed on the machine itself.
                </div>
              </div>

              <div className="space-y-1.5">
                <div className="font-semibold">Configured &amp; Discovered Peers</div>
                {peersStatus.map((p, idx) => (
                  <div
                    key={`${p.id}:${p.url}`}
                    className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2"
                    style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                  >
                    <div>
                      <span className="font-semibold">{p.name}</span>{" "}
                      <span className="font-mono-code text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                        ({p.url})
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span style={{ color: p.online ? "var(--accent-emerald)" : "var(--text-muted)" }}>
                        {p.online ? "Online" : "Offline"}
                      </span>
                      <button
                        onClick={() =>
                          setDraft({
                            ...draft,
                            peers: draft.peers.filter((_, i) => i !== idx),
                          })
                        }
                        className="p-1 text-red-400 cursor-pointer"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <input
                    value={newPeerName}
                    onChange={(e) => setNewPeerName(e.target.value)}
                    placeholder="Peer name (e.g. desktop)"
                    className="w-full sm:w-44 rounded border px-2 py-1"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <input
                    value={newPeerUrl}
                    onChange={(e) => setNewPeerUrl(e.target.value)}
                    placeholder="http://192.168.1.x:8790"
                    className="flex-1 min-w-[11.25rem] rounded border px-2 py-1 font-mono-code"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <button
                    onClick={() => {
                      if (!newPeerName.trim() || !newPeerUrl.trim()) return;
                      let rawUrl = newPeerUrl.trim().replace(/\/+$/, "");
                      if (!/^https?:\/\//i.test(rawUrl)) rawUrl = `http://${rawUrl}`;
                      try {
                        const u = new URL(rawUrl);
                        if (!u.port) u.port = "8790";
                        rawUrl = u.toString().replace(/\/+$/, "");
                      } catch {
                        // Keep rawUrl if URL parsing fails
                      }
                      setDraft({
                        ...draft,
                        peers: [
                          ...draft.peers,
                          { id: newPeerName.trim().toLowerCase(), name: newPeerName.trim(), url: rawUrl, enabled: true },
                        ],
                      });
                      setNewPeerName("");
                    }}
                    className="inline-flex items-center gap-1 px-2.5 py-1 rounded font-semibold cursor-pointer"
                    style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}
                  >
                    <Plus className="w-3.5 h-3.5" /> Add
                  </button>
                </div>
              </div>
            </div>
          )}

          {tab === "gpu" && (
            <div className="space-y-3">
              <label className="inline-flex items-center gap-2 font-medium">
                <input
                  type="checkbox"
                  checked={draft.paths.useGpuScheduler}
                  onChange={(e) => setDraft({ ...draft, paths: { ...draft.paths, useGpuScheduler: e.target.checked } })}
                />
                Use gpu-scheduler when running ({draft.paths.gpuSchedulerUrl})
              </label>
              {(["vaultDir", "githubDir", "llmBaseUrl", "comfyUrl", "gpuSchedulerUrl", "agyDir", "claudeDir", "toolsDir"] as const).map((k) => (
                <div key={k} className="space-y-1">
                  <label className="font-mono-code text-[0.6875rem]" style={{ color: "var(--text-secondary)" }}>
                    {k}
                  </label>
                  <input
                    value={draft.paths[k] || ""}
                    onChange={(e) => setDraft({ ...draft, paths: { ...draft.paths, [k]: e.target.value } })}
                    className="w-full rounded border px-2.5 py-1.5 font-mono-code"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                </div>
              ))}
            </div>
          )}

          {tab === "tools" && (
            <div className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <label className="inline-flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={draft.terminal.askApprovalBeforeRun}
                    onChange={(e) =>
                      setDraft({ ...draft, terminal: { ...draft.terminal, askApprovalBeforeRun: e.target.checked } })
                    }
                  />
                  Require confirmation for agent-proposed terminal scripts
                </label>
                <div className="flex items-center gap-2">
                  <span>Sudo pop-up timeout (s):</span>
                  <input
                    type="number"
                    value={draft.terminal.sudoTimeoutSec}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        terminal: { ...draft.terminal, sudoTimeoutSec: Number(e.target.value) || 60 },
                      })
                    }
                    className="w-20 rounded border px-2 py-1 font-mono-code"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                </div>
              </div>
              <div className="space-y-1.5">
                <div className="font-semibold">Python Tools Suite ({tools.length})</div>
                {tools.map((t) => {
                  const enabled = !draft.disabledTools.includes(t.name);
                  return (
                    <label
                      key={t.name}
                      className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 cursor-pointer"
                      style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                    >
                      <div>
                        <span className="font-semibold font-mono-code">{t.name}</span>{" "}
                        <span className="text-[0.625rem] px-1.5 py-0.2 rounded" style={{ backgroundColor: "var(--accent-primary-soft)", color: "var(--accent-primary)" }}>
                          {t.category} · GPU: {t.gpuNeed}
                        </span>
                        <div className="text-[0.6875rem]" style={{ color: "var(--text-secondary)" }}>
                          {t.description}
                        </div>
                      </div>
                      <input
                        type="checkbox"
                        checked={enabled}
                        onChange={() =>
                          setDraft({
                            ...draft,
                            disabledTools: enabled
                              ? [...draft.disabledTools, t.name]
                              : draft.disabledTools.filter((x) => x !== t.name),
                          })
                        }
                      />
                    </label>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        <div className="px-4 py-3 border-t flex justify-end gap-2 shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <button onClick={onClose} className="px-3 py-1.5 rounded-md border text-xs cursor-pointer" style={{ borderColor: "var(--border-subtle)" }}>
            Cancel
          </button>
          <button
            onClick={async () => {
              await onSave(draft);
              onClose();
            }}
            className="px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer"
            style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}
          >
            Save Settings
          </button>
        </div>
      </div>
    </div>
  );
}
