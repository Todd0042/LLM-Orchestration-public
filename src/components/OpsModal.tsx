import { useEffect, useState } from "react";
import {
  FolderGit2,
  HardDrive,
  RefreshCw,
  Upload,
  Wrench,
  X,
} from "lucide-react";
import type {
  ArtifactItem,
  IntermissionEvent,
  RepoComparisonRow,
  ToolInventoryRow,
  VaultFileItem,
} from "@/types/orch";
import {
  fetchRepoInventory,
  fetchToolInventory,
  fetchVaultList,
  syncRepoAction,
  triggerDesktopCatchup,
  writeVaultFile,
} from "@/lib/api";
import { usePlugins } from "@/plugins";

/** Built-in tabs, or the id of a plugin (src/plugins). */
type OpsTab = "repos" | "vault" | "tools" | (string & {});

interface OpsModalProps {
  open: boolean;
  /** Render inside a page/sheet instead of as its own pop-up window. */
  embedded?: boolean;
  initialTab?: OpsTab;
  desktopOnline: boolean;
  activeChatId: string;
  onOpenArtifact: (a: ArtifactItem) => void;
  onRefreshChats: () => void;
  onClose: () => void;
}

export function OpsModal({
  open,
  embedded = false,
  initialTab = "vault",
  desktopOnline,
  activeChatId,
  onOpenArtifact,
  onRefreshChats,
  onClose,
}: OpsModalProps) {
  const [tab, setTab] = useState<OpsTab>(initialTab);
  const [comparison, setComparison] = useState<RepoComparisonRow[]>([]);
  const [queue, setQueue] = useState<IntermissionEvent[]>([]);
  const [toolRows, setToolRows] = useState<ToolInventoryRow[]>([]);
  const [vaultDir, setVaultDir] = useState("");
  const [vaultFiles, setVaultFiles] = useState<VaultFileItem[]>([]);
  const [newVaultName, setNewVaultName] = useState("");
  const [newVaultBody, setNewVaultBody] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const plugins = usePlugins();
  const activePlugin = plugins.find((p) => p.id === tab);

  const loadTab = async (t: typeof tab, refresh = false) => {
    setBusy(true);
    try {
      if (t === "repos") {
        const res = await fetchRepoInventory(refresh);
        setComparison(res.comparison);
        setQueue(res.queue);
      } else if (t === "vault") {
        const res = await fetchVaultList();
        setVaultDir(res.vaultDir);
        setVaultFiles(res.files);
      } else if (t === "tools") {
        const res = await fetchToolInventory();
        setToolRows(res.items);
      }
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (open) void loadTab(tab, false);
  }, [open, tab]);

  useEffect(() => {
    if (!open || embedded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const badgeColor = (st: RepoComparisonRow["status"]) => {
    if (st === "in_sync") return "var(--accent-emerald)";
    if (st === "dirty" || st === "diverged" || st === "branch_mismatch") return "var(--accent-crimson)";
    return "var(--accent-amber)";
  };

  return (
    <div
      className={embedded ? "h-full w-full flex flex-col min-h-0" : "fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-xs p-2 lg:p-4"}
      onClick={embedded ? undefined : onClose}
    >
      <div
        className={embedded ? "w-full h-full flex flex-col overflow-hidden min-h-0" : "w-full h-full max-w-[calc(100vw-16px)] max-h-[calc(100dvh-16px)] lg:h-[90dvh] lg:max-w-5xl flex flex-col rounded-xl border shadow-2xl overflow-hidden"}
        style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-strong)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {!embedded && (
        <div className="px-4 py-2.5 border-b flex flex-wrap items-center justify-between gap-2 shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <h3 className="text-sm font-semibold min-w-0 truncate">More tools</h3>
          <div className="flex items-center gap-2">
            <button
              onClick={() => void loadTab(tab, true)}
              disabled={busy}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border text-[0.6875rem] cursor-pointer"
              style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
            >
              <RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} />
              Rescan
            </button>
            <button
              onClick={onClose}
              title="Close Hub Operations (Esc)"
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded border text-[0.6875rem] font-medium hover:opacity-90 cursor-pointer"
              style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
            >
              <X className="w-3.5 h-3.5" />
              Close
            </button>
          </div>
        </div>

        )}

        <div className="px-3 pt-2 border-b flex flex-wrap items-center gap-1 text-xs" style={{ borderColor: "var(--border-subtle)" }}>
          {(
            [
              ["vault", "Vault", HardDrive],
              ["repos", "Catch-up queue", FolderGit2],
              ...plugins.map((p) => [p.id, p.label, p.icon] as const),
              ["tools", "Models & tools per machine", Wrench],
            ] as const
          ).map(([k, lbl, Icon]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-t-md border-b-2 font-medium cursor-pointer"
              style={{
                borderColor: tab === k ? "var(--accent-primary)" : "transparent",
                color: tab === k ? "var(--accent-primary)" : "var(--text-secondary)",
              }}
            >
              <Icon className="w-3.5 h-3.5" />
              {lbl}
            </button>
          ))}
        </div>

        {statusMsg && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border text-xs flex items-center justify-between" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--accent-primary)" }}>
            <span>{statusMsg}</span>
            <button onClick={() => setStatusMsg("")} className="text-[0.6875rem] underline cursor-pointer">
              Dismiss
            </button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-4 space-y-4 text-sm lg:text-xs">
          {tab === "repos" && (
            <div className="space-y-4">
              <div className="rounded-lg border p-3 flex flex-wrap items-center justify-between gap-2" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div>
                  <div className="font-semibold">
                    Desktop Intermission Queue ({queue.filter((q) => q.status === "queued").length} queued · {queue.length} total)
                  </div>
                  <div className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
                    {desktopOnline
                      ? "Desktop is online. Queued offline changes sync automatically or on click."
                      : "Desktop is offline. Laptop Hub logs repo/vault/tool updates and catches Desktop up first on reconnect."}
                  </div>
                </div>
                <button
                  disabled={!desktopOnline || busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      const res = await triggerDesktopCatchup();
                      setStatusMsg(`Catch-up finished: ${res.synced} synced, ${res.conflicts} needing agent.`);
                      await loadTab("repos", true);
                    } finally {
                      setBusy(false);
                    }
                  }}
                  className="px-3 py-1.5 rounded-md font-semibold cursor-pointer disabled:opacity-40"
                  style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}
                >
                  Run Catch-Up Now
                </button>
              </div>

              {queue.length > 0 && (
                <div className="space-y-1.5">
                  <div className="font-semibold">Recent Intermission Events</div>
                  {queue.slice(0, 12).map((q) => (
                    <div
                      key={q.id}
                      className="rounded-lg border px-3 py-2 flex flex-wrap items-center justify-between gap-2"
                      style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                    >
                      <div>
                        <span className="font-mono-code font-semibold">{q.target}</span>{" "}
                        <span className="text-[0.625rem] px-1.5 py-0.5 rounded" style={{ backgroundColor: "var(--bg-elevated)", color: "var(--text-secondary)" }}>
                          {q.category}
                        </span>
                        {q.resultNote && (
                          <div className="text-[0.6875rem] mt-0.5" style={{ color: "var(--text-secondary)" }}>
                            {q.resultNote}
                          </div>
                        )}
                      </div>
                      <span className="font-mono-code text-[0.625rem] uppercase" style={{ color: q.status === "synced" ? "var(--accent-emerald)" : q.status === "conflict_needs_agent" ? "var(--accent-crimson)" : "var(--accent-amber)" }}>
                        {q.status}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              <div className="space-y-1.5">
                <div className="font-semibold">GitHub Repos Comparison ({comparison.length})</div>
                {comparison.map((row) => (
                  <div
                    key={row.repoName}
                    className="rounded-lg border p-2.5 flex flex-wrap items-center justify-between gap-2"
                    style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                  >
                    <div className="min-w-[13.75rem]">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold font-mono-code">{row.repoName}</span>
                        <span className="text-[0.625rem] font-semibold uppercase" style={{ color: badgeColor(row.status) }}>
                          {row.status.replace("_", " ")}
                        </span>
                      </div>
                      <div className="text-[0.6875rem] font-mono-code mt-0.5" style={{ color: "var(--text-muted)" }}>
                        Laptop: {row.laptop ? `${row.laptop.branch}@${row.laptop.headSha} (dirty ${row.laptop.dirtyCount})` : "—"} · Desktop:{" "}
                        {row.desktop ? `${row.desktop.branch}@${row.desktop.headSha} (dirty ${row.desktop.dirtyCount})` : "—"}
                      </div>
                    </div>

                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={async () => {
                          setBusy(true);
                          try {
                            const r = await syncRepoAction({ repoName: row.repoName, action: "pull", targetNode: "both" });
                            setStatusMsg(`Pull ${row.repoName}: ${JSON.stringify(r)}`);
                            await loadTab("repos", true);
                          } finally {
                            setBusy(false);
                          }
                        }}
                        className="px-2 py-1 rounded border text-[0.6875rem] cursor-pointer"
                        style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                      >
                        Pull Clean
                      </button>
                      <button
                        onClick={async () => {
                          setBusy(true);
                          try {
                            await syncRepoAction({
                              repoName: row.repoName,
                              action: "reconcile",
                              targetNode: "laptop",
                              agent: "agy-cli",
                              chatId: activeChatId,
                            });
                            setStatusMsg(`Asked AGY to inspect & reconcile ${row.repoName}.`);
                            onRefreshChats();
                          } finally {
                            setBusy(false);
                          }
                        }}
                        className="px-2 py-1 rounded text-[0.6875rem] font-semibold cursor-pointer"
                        style={{ backgroundColor: "var(--accent-primary-soft)", color: "var(--accent-primary)" }}
                      >
                        Agent Reconcile
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "vault" && (
            <div className="space-y-4">
              <div className="rounded-lg border p-3 space-y-2" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-semibold">Shared Hub Vault</div>
                    <div className="text-[0.6875rem] font-mono-code" style={{ color: "var(--text-muted)" }}>
                      {vaultDir || "~/LLM-Vault"}
                    </div>
                  </div>
                  <label className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded border cursor-pointer text-[0.6875rem]" style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-strong)" }}>
                    <Upload className="w-3.5 h-3.5" />
                    Upload File
                    <input
                      type="file"
                      className="hidden"
                      onChange={async (e) => {
                        const f = e.target.files?.[0];
                        if (!f) return;
                        const buf = await f.arrayBuffer();
                        let bin = "";
                        new Uint8Array(buf).forEach((b) => {
                          bin += String.fromCharCode(b);
                        });
                        await writeVaultFile({ relPath: f.name, contentBase64: btoa(bin) });
                        await loadTab("vault", false);
                      }}
                    />
                  </label>
                </div>
                <div className="flex flex-col sm:flex-row gap-2">
                  <input
                    value={newVaultName}
                    onChange={(e) => setNewVaultName(e.target.value)}
                    placeholder="plans/my-app-plan.md"
                    className="sm:w-64 rounded border px-2 py-1 font-mono-code"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <input
                    value={newVaultBody}
                    onChange={(e) => setNewVaultBody(e.target.value)}
                    placeholder="Quick note / plan snippet to save into Vault…"
                    className="flex-1 rounded border px-2 py-1"
                    style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-subtle)" }}
                  />
                  <button
                    onClick={async () => {
                      if (!newVaultName.trim()) return;
                      await writeVaultFile({ relPath: newVaultName.trim(), content: newVaultBody });
                      setNewVaultName("");
                      setNewVaultBody("");
                      await loadTab("vault", false);
                    }}
                    className="px-3 py-1 rounded font-semibold cursor-pointer"
                    style={{ backgroundColor: "var(--accent-primary)", color: "#0b0e14" }}
                  >
                    Save
                  </button>
                </div>
              </div>

              <div className="space-y-1">
                {vaultFiles.length === 0 ? (
                  <div className="text-center py-6" style={{ color: "var(--text-muted)" }}>
                    Vault is empty. Save plans, diffs, or reports here so agents on every machine can share them.
                  </div>
                ) : (
                  vaultFiles.map((vf) => (
                    <button
                      key={vf.relPath}
                      onClick={() => {
                        const isHtml = vf.relPath.endsWith(".html") || vf.relPath.endsWith(".htm");
                        onOpenArtifact({
                          id: `vault_${vf.relPath}`,
                          kind: isHtml ? "html" : "doc",
                          title: vf.relPath,
                          path: vf.absPath,
                          nodeId: "laptop",
                          mimeType: vf.mimeType,
                          meta: { origin: "vault" },
                        });
                        onClose();
                      }}
                      className="w-full text-left rounded-lg border px-3 py-2 flex items-center justify-between gap-2 cursor-pointer"
                      style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                    >
                      <span className="font-mono-code truncate">{vf.relPath}</span>
                      <span className="text-[0.625rem] font-mono-code shrink-0" style={{ color: "var(--text-muted)" }}>
                        {(vf.size / 1024).toFixed(1)} KB
                      </span>
                    </button>
                  ))
                )}
              </div>
            </div>
          )}

          {activePlugin && (
            <activePlugin.Panel activeChatId={activeChatId} onOpenArtifact={onOpenArtifact} onClose={onClose} />
          )}

          {tab === "tools" && (
            <div className="space-y-1.5">
              <div className="font-semibold">Per-Node Tool Inventory ({toolRows.length})</div>
              {toolRows.map((t) => (
                <div
                  key={`${t.nodeId}:${t.toolId}`}
                  className="rounded-lg border px-3 py-2 flex items-center justify-between gap-2"
                  style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
                >
                  <div>
                    <span className="font-semibold">{t.name}</span>{" "}
                    <span className="text-[0.625rem] font-mono-code px-1.5 py-0.5 rounded" style={{ backgroundColor: "var(--bg-elevated)", color: "var(--accent-primary)" }}>
                      @{t.nodeId} · {t.kind}
                    </span>
                  </div>
                  <span className="text-[0.625rem] font-mono-code uppercase" style={{ color: "var(--accent-emerald)" }}>
                    {t.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
