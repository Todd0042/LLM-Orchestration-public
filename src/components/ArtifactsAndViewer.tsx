import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { FileCode2, FileDiff, FileText, Film, Globe, Image, Music } from "lucide-react";
import type { ArtifactItem } from "@/types/orch";
import { buildMediaUrl, fetchFileContent } from "@/lib/api";

interface ArtifactsAndViewerProps {
  artifacts: ArtifactItem[];
  selectedArtifact: ArtifactItem | null;
  onSelectArtifact: (a: ArtifactItem) => void;
}

export function ArtifactsAndViewer({
  artifacts,
  selectedArtifact,
  onSelectArtifact,
}: ArtifactsAndViewerProps) {
  const [tab, setTab] = useState<"all" | "doc" | "diff" | "media">("all");
  const [fileText, setFileText] = useState<string>("");
  const [mediaSrc, setMediaSrc] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [showRawHtml, setShowRawHtml] = useState(false);

  const filtered = artifacts.filter((a) => {
    if (tab === "all") return true;
    if (tab === "media") return a.kind === "image" || a.kind === "video" || a.kind === "audio";
    if (tab === "doc") return a.kind === "doc" || a.kind === "html" || a.kind === "file";
    return a.kind === tab;
  });

  useEffect(() => {
    let cancelled = false;
    setShowRawHtml(false);
    if (!selectedArtifact) {
      setFileText("");
      setMediaSrc("");
      return;
    }
    if (selectedArtifact.kind === "image" || selectedArtifact.kind === "video" || selectedArtifact.kind === "audio") {
      setLoading(true);
      buildMediaUrl(selectedArtifact.path, selectedArtifact.nodeId)
        .then((u) => {
          if (!cancelled) setMediaSrc(u);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
      return;
    }
    if (selectedArtifact.kind === "diff" && (selectedArtifact.meta?.oldText || selectedArtifact.meta?.newText)) {
      setFileText("");
      return;
    }
    setLoading(true);
    fetchFileContent(selectedArtifact.path, selectedArtifact.nodeId)
      .then((res) => {
        if (!cancelled) setFileText(res.content);
      })
      .catch((err) => {
        if (!cancelled) setFileText(`Unable to read file: ${String(err)}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedArtifact]);

  const iconFor = (k: ArtifactItem["kind"]) => {
    if (k === "diff") return <FileDiff className="w-3.5 h-3.5" style={{ color: "var(--accent-primary)" }} />;
    if (k === "html") return <Globe className="w-3.5 h-3.5" style={{ color: "var(--accent-amber)" }} />;
    if (k === "image") return <Image className="w-3.5 h-3.5" style={{ color: "var(--accent-emerald)" }} />;
    if (k === "video") return <Film className="w-3.5 h-3.5" style={{ color: "var(--accent-violet)" }} />;
    if (k === "audio") return <Music className="w-3.5 h-3.5" style={{ color: "var(--accent-amber)" }} />;
    if (k === "doc") return <FileText className="w-3.5 h-3.5" style={{ color: "var(--accent-primary)" }} />;
    return <FileCode2 className="w-3.5 h-3.5" style={{ color: "var(--text-secondary)" }} />;
  };

  const isHtmlDoc =
    selectedArtifact?.kind === "html" ||
    selectedArtifact?.path.toLowerCase().endsWith(".html") ||
    selectedArtifact?.path.toLowerCase().endsWith(".htm");

  return (
    <PanelGroup direction="vertical" autoSaveId="llm_orch_right_split_v1">
      {/* Top: Organized Artifacts */}
      <Panel defaultSize={40} minSize={20}>
        <div
          className="h-full flex flex-col border-b overflow-hidden"
          style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-subtle)" }}
        >
          <div className="px-3 py-2 border-b flex items-center justify-between gap-2" style={{ borderColor: "var(--border-subtle)" }}>
            <span className="text-xs font-semibold">Artifacts ({artifacts.length})</span>
            <div className="flex items-center gap-1 text-[0.625rem]">
              {(
                [
                  ["all", "All"],
                  ["doc", "Docs & Reports"],
                  ["diff", "Diffs"],
                  ["media", "Media"],
                ] as const
              ).map(([k, lbl]) => (
                <button
                  key={k}
                  onClick={() => setTab(k)}
                  className="px-2 py-0.5 rounded font-medium cursor-pointer"
                  style={{
                    backgroundColor: tab === k ? "var(--accent-primary-soft)" : "var(--bg-muted)",
                    color: tab === k ? "var(--accent-primary)" : "var(--text-secondary)",
                  }}
                >
                  {lbl}
                </button>
              ))}
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {filtered.length === 0 ? (
              <div className="h-full flex items-center justify-center text-xs" style={{ color: "var(--text-muted)" }}>
                No artifacts in this filter.
              </div>
            ) : (
              filtered.map((a) => {
                const active = selectedArtifact?.path === a.path && selectedArtifact?.kind === a.kind;
                return (
                  <button
                    key={a.id}
                    onClick={() => onSelectArtifact(a)}
                    className="w-full text-left px-2.5 py-1.5 rounded-lg border flex items-center justify-between gap-2 cursor-pointer"
                    style={{
                      backgroundColor: active ? "var(--bg-elevated)" : "transparent",
                      borderColor: active ? "var(--accent-primary)" : "var(--border-subtle)",
                    }}
                  >
                    <span className="inline-flex items-center gap-2 min-w-0">
                      {iconFor(a.kind)}
                      <span className="text-xs font-medium truncate">{a.title}</span>
                    </span>
                    <span className="text-[0.625rem] font-mono-code shrink-0" style={{ color: "var(--text-muted)" }}>
                      @{a.nodeId}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      </Panel>

      <PanelResizeHandle
        className="h-1 hover:bg-sky-500/40 transition-colors cursor-row-resize"
        style={{ backgroundColor: "var(--border-subtle)" }}
      />

      {/* Bottom: Context-Aware Diff, HTML Report & Media Viewer */}
      <Panel defaultSize={60} minSize={25}>
        <div className="h-full flex flex-col overflow-hidden" style={{ backgroundColor: "var(--bg-canvas)" }}>
          <div
            className="px-3 py-1.5 border-b flex items-center justify-between gap-2 text-xs shrink-0"
            style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}
          >
            <span className="font-semibold truncate">
              {selectedArtifact ? selectedArtifact.title : "Diffs, Reports & Media Viewer"}
            </span>
            <div className="flex items-center gap-2 min-w-0">
              {isHtmlDoc && fileText && (
                <button
                  type="button"
                  onClick={() => setShowRawHtml((v) => !v)}
                  className="text-[0.625rem] px-2 py-0.5 rounded border cursor-pointer shrink-0"
                  style={{ borderColor: "var(--border-strong)", color: "var(--accent-primary)" }}
                >
                  {showRawHtml ? "Rendered HTML" : "Raw Source"}
                </button>
              )}
              {selectedArtifact && (
                <span className="text-[0.625rem] font-mono-code truncate max-w-[12.5rem]" style={{ color: "var(--text-muted)" }}>
                  {selectedArtifact.path}
                </span>
              )}
            </div>
          </div>

          <div className={`flex-1 overflow-auto ${isHtmlDoc && !showRawHtml && !loading ? "p-0" : "p-3"}`}>
            {!selectedArtifact ? (
              <div className="h-full flex items-center justify-center text-xs p-3" style={{ color: "var(--text-muted)" }}>
                Click any artifact above, report, Vault file, or diff link in chat to inspect it here.
              </div>
            ) : loading ? (
              <div className="text-xs" style={{ color: "var(--text-muted)" }}>
                Loading…
              </div>
            ) : selectedArtifact.kind === "image" ? (
              <div className="flex items-center justify-center h-full">
                <img src={mediaSrc} alt={selectedArtifact.title} className="max-h-full max-w-full rounded-lg border object-contain" />
              </div>
            ) : selectedArtifact.kind === "video" ? (
              <video src={mediaSrc} controls className="w-full max-h-full rounded-lg border" />
            ) : selectedArtifact.kind === "audio" ? (
              <div className="h-full flex flex-col items-center justify-center gap-3">
                <Music className="w-10 h-10" style={{ color: "var(--accent-amber)" }} />
                <div className="text-xs font-semibold">{selectedArtifact.title}</div>
                <audio src={mediaSrc} controls className="w-full max-w-md" />
              </div>
            ) : selectedArtifact.kind === "diff" &&
              (selectedArtifact.meta?.oldText || selectedArtifact.meta?.newText) ? (
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-2 font-mono-code text-[0.6875rem]">
                <div className="rounded-lg border overflow-hidden" style={{ borderColor: "var(--border-subtle)" }}>
                  <div className="px-2 py-1 text-[0.625rem] font-semibold bg-red-500/15 text-red-300">Before</div>
                  <pre className="p-2.5 whitespace-pre-wrap overflow-x-auto">{selectedArtifact.meta.oldText || "(new file)"}</pre>
                </div>
                <div className="rounded-lg border overflow-hidden" style={{ borderColor: "var(--border-subtle)" }}>
                  <div className="px-2 py-1 text-[0.625rem] font-semibold bg-emerald-500/15 text-emerald-300">After</div>
                  <pre className="p-2.5 whitespace-pre-wrap overflow-x-auto">{selectedArtifact.meta.newText || "(deleted)"}</pre>
                </div>
              </div>
            ) : isHtmlDoc && !showRawHtml ? (
              <iframe
                title={selectedArtifact.title}
                sandbox=""
                srcDoc={fileText}
                className="w-full h-full border-0 bg-[#0b0e14]"
              />
            ) : (
              <pre className="text-xs font-mono-code whitespace-pre-wrap leading-relaxed" style={{ color: "var(--text-primary)" }}>
                {fileText}
              </pre>
            )}
          </div>
        </div>
      </Panel>
    </PanelGroup>
  );
}
