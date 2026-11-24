import { useEffect, useState } from "react";
import { fetchFeatures } from "@/lib/api";
import type { UiPlugin } from "./types";

export type { PluginPanelProps, UiPlugin } from "./types";

// Every folder in src/plugins with an index.tsx is bundled. Plugins that aren't checked out simply aren't here.
const modules = import.meta.glob<{ plugin: UiPlugin }>("./*/index.tsx", { eager: true });
const ALL: UiPlugin[] = Object.values(modules)
  .map((m) => m.plugin)
  .filter(Boolean)
  .sort((a, b) => a.label.localeCompare(b.label));

let enabledIds: Promise<Set<string>> | null = null;
function loadEnabled(): Promise<Set<string>> {
  enabledIds ??= fetchFeatures()
    .then((r) => new Set(r.features.map((f) => f.id)))
    .catch(() => {
      enabledIds = null; // try again next time (e.g. before sign-in)
      return new Set<string>();
    });
  return enabledIds;
}

/** The UI plugins whose server side is installed on the node we're talking to. */
export function usePlugins(): UiPlugin[] {
  const [ids, setIds] = useState<Set<string> | null>(null);
  useEffect(() => {
    let live = true;
    if (ALL.length) void loadEnabled().then((s) => live && setIds(s));
    return () => {
      live = false;
    };
  }, []);
  return ids ? ALL.filter((p) => ids.has(p.id)) : [];
}

export function findPlugin(id: string): UiPlugin | undefined {
  return ALL.find((p) => p.id === id);
}
