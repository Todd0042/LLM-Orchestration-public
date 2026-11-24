import type { DropdownOption } from "@/components/ui/Dropdown";
import type { ModelInfo, RepoComparisonRow } from "@/types/orch";

const RECOMMENDED_LOCAL_ORDER: Record<string, number> = {
  "gpt-oss:20b": 0,
  "qwen3:30b-a3b": 1,
  "qwen2.5-coder:14b": 2,
  "qwen3-8b": 3,
  "qwen3-14b": 4,
};

const AGY_PINNED_ORDER: Record<string, number> = {
  "gemini-4-neon-high": 0,
  "gemini-4-argon-high": 1,
  "gemini-4-hydrogen-high": 2,
  "gemini-3.8-flash-high": 3,
  "claude-opus-5-5-high": 4,
  "claude-sonnet-5-5-high": 5,
};

const TIER_ORDER: Record<string, number> = {
  low: 0,
  medium: 1,
  high: 2,
  xhigh: 3,
  max: 4,
};

function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function getOrchestratorRank(m: ModelInfo, preferredCliNode: string): { bucket: number; group: string; priority: number } {
  if (m.id === "agy-cli") {
    return { bucket: 0, group: "CLI Agents", priority: m.nodeId === preferredCliNode ? 0 : 1 };
  }
  if (m.id === "claude-cli") {
    return { bucket: 0, group: "CLI Agents", priority: m.nodeId === preferredCliNode ? 2 : 3 };
  }
  if (m.id in RECOMMENDED_LOCAL_ORDER) {
    return { bucket: 1, group: "Top Recommended", priority: RECOMMENDED_LOCAL_ORDER[m.id] };
  }
  return { bucket: 2, group: "All Local Models (A–Z)", priority: 0 };
}

/** Sort orchestrator models cleanly: AGY -> Claude -> Top Recommended -> Alphanumeric A–Z. */
export function sortOrchestratorModels(models: ModelInfo[], preferredCliNode = "laptop"): ModelInfo[] {
  const usable = models.filter((m) => m.kind === "llm" && !m.id.toLowerCase().includes("embed"));
  return [...usable].sort((a, b) => {
    const ra = getOrchestratorRank(a, preferredCliNode);
    const rb = getOrchestratorRank(b, preferredCliNode);
    if (ra.bucket !== rb.bucket) return ra.bucket - rb.bucket;
    if (ra.priority !== rb.priority) return ra.priority - rb.priority;
    const byName = naturalCompare(a.displayName || a.id, b.displayName || b.id);
    if (byName !== 0) return byName;
    return naturalCompare(a.nodeId, b.nodeId);
  });
}

export function buildOrchestratorOptions(
  models: ModelInfo[],
  preferredCliNode = "laptop",
  options?: { valueMode?: "composite" | "id"; includeHint?: boolean; labelPrefix?: string }
): DropdownOption[] {
  const valueMode = options?.valueMode ?? "composite";
  const includeHint = options?.includeHint ?? true;
  const prefix = options?.labelPrefix ?? "";
  const sorted = sortOrchestratorModels(models, preferredCliNode);

  const seenIds = new Set<string>();
  const out: DropdownOption[] = [];
  for (const m of sorted) {
    if (valueMode === "id" && seenIds.has(m.id)) continue;
    seenIds.add(m.id);
    const rank = getOrchestratorRank(m, preferredCliNode);
    const badge =
      m.id === "agy-cli" || m.id === "claude-cli"
        ? "CLI"
        : rank.bucket === 1
          ? m.nodeId === "laptop"
            ? "Hub Pick"
            : "Recommended"
          : m.strengths[0];
    out.push({
      value: valueMode === "composite" ? `${m.nodeId}::${m.id}` : m.id,
      label: `${prefix}${m.displayName} [${m.nodeId}]`,
      badge,
      group: rank.group,
      hint: includeHint ? `${m.strengths.join(" · ")} — ${m.fit}` : undefined,
    });
  }
  return out;
}

/** Sort AGY sub-models: Flagships first, then alphanumeric family & effort tier. */
export function sortAgyModels(items: Array<{ id: string; label: string }>): Array<{ id: string; label: string }> {
  return [...items].sort((a, b) => {
    const pa = AGY_PINNED_ORDER[a.id];
    const pb = AGY_PINNED_ORDER[b.id];
    if (pa !== undefined || pb !== undefined) {
      if (pa === undefined) return 1;
      if (pb === undefined) return -1;
      return pa - pb;
    }
    const baseA = a.id.replace(/-(low|medium|high|xhigh|max)$/, "");
    const baseB = b.id.replace(/-(low|medium|high|xhigh|max)$/, "");
    const byBase = naturalCompare(baseA, baseB);
    if (byBase !== 0) return byBase;
    const tierA = TIER_ORDER[a.id.match(/-(low|medium|high|xhigh|max)$/)?.[1] || ""] ?? 99;
    const tierB = TIER_ORDER[b.id.match(/-(low|medium|high|xhigh|max)$/)?.[1] || ""] ?? 99;
    if (tierA !== tierB) return tierA - tierB;
    return naturalCompare(a.label, b.label);
  });
}

export function sortModelsAlpha(models: ModelInfo[], kind: ModelInfo["kind"]): ModelInfo[] {
  return models
    .filter((m) => m.kind === kind)
    .sort((a, b) => {
      const byName = naturalCompare(a.displayName || a.id, b.displayName || b.id);
      return byName !== 0 ? byName : naturalCompare(a.nodeId, b.nodeId);
    });
}

export function sortReposAlpha(rows: RepoComparisonRow[]): RepoComparisonRow[] {
  return [...rows].sort((a, b) => naturalCompare(a.repoName, b.repoName));
}
