import type { EffortLevel } from "@/types/orch";
import { buildOrchestratorOptions, sortAgyModels } from "@/lib/modelSort";
import { setModel } from "@/state/actions";
import { setState, useApp } from "@/state/store";
import { Dropdown } from "@/components/ui/Dropdown";

const EFFORTS: EffortLevel[] = ["low", "medium", "high", "xhigh", "max"];

/** Who answers: a local model on a machine, or AGY / Claude (with their model and thinking level). */
export function ModelPicker() {
  const models = useApp((s) => s.models);
  const catalog = useApp((s) => s.engineCatalog);
  const settings = useApp((s) => s.settings);
  const modelId = useApp((s) => s.modelId);
  const nodeId = useApp((s) => s.nodeId);
  const engineModel = useApp((s) => s.engineModel);
  const effort = useApp((s) => s.effort);
  const agyMode = useApp((s) => s.agyMode);
  const local = settings?.node.id ?? "desktop";
  const isAgy = modelId === "agy-cli";
  const isClaude = modelId === "claude-cli";

  const engineOptions = isAgy
    ? sortAgyModels(catalog?.agyModels ?? []).map((m) => ({ value: m.id, label: m.label }))
    : (catalog?.claudePresets ?? [{ id: "opus", label: "Claude Opus" }, { id: "sonnet", label: "Claude Sonnet" }, { id: "haiku", label: "Claude Haiku" }]).map((m) => ({ value: m.id, label: m.label }));

  return (
    <>
      <Dropdown
        title="Who answers"
        value={`${nodeId}::${modelId}`}
        onChange={(v) => {
          const [n, ...rest] = v.split("::");
          setModel(rest.join("::"), n);
        }}
        placement="up"
        menuWidthClass="min-w-[18.75rem]"
        options={buildOrchestratorOptions(models, local)}
        placeholder="Choose a model"
      />
      {(isAgy || isClaude) && (
        <>
          <Dropdown
            title="Agent model"
            value={engineModel || (isAgy ? settings?.engines?.agy.model || "gemini-4-neon-high" : settings?.engines?.claude.model || "opus")}
            onChange={(v) => {
              const tier = isAgy ? v.match(/-(xhigh|medium|high|low|max)$/)?.[1] : undefined;
              setState({ engineModel: v, ...(tier ? { effort: tier as EffortLevel } : {}) });
            }}
            placement="up"
            menuWidthClass="min-w-[15.0rem]"
            options={engineOptions}
          />
          <Dropdown
            title="Thinking"
            value={effort || "high"}
            onChange={(v) => setState({ effort: v as EffortLevel })}
            placement="up"
            options={EFFORTS.map((e) => ({ value: e, label: `Thinking: ${e}` }))}
          />
          {isAgy && (
            <Dropdown
              title="AGY mode"
              value={agyMode || "accept-edits"}
              onChange={(v) => setState({ agyMode: v as typeof agyMode })}
              placement="up"
              options={[
                { value: "accept-edits", label: "Edits: accept" },
                { value: "auto", label: "Edits: auto" },
                { value: "plan", label: "Plan only" },
              ]}
            />
          )}
        </>
      )}
    </>
  );
}
