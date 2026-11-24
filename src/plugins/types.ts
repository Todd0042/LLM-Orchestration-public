import type { ComponentType } from "react";
import type { ArtifactItem } from "@/types/orch";

/** What a plugin's screen gets from the Ops page that hosts it. */
export interface PluginPanelProps {
  activeChatId: string;
  onOpenArtifact: (a: ArtifactItem) => void;
  onClose: () => void;
}

/**
 * A UI plugin: `src/plugins/<id>/index.tsx` exporting `plugin`. It's shown only when the server reports a
 * feature with the same id (GET /api/features), i.e. when the plugin's server side is installed too.
 */
export interface UiPlugin {
  id: string;
  label: string;
  hint: string;
  icon: ComponentType<{ className?: string }>;
  Panel: ComponentType<PluginPanelProps>;
}
