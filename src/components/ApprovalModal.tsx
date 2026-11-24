import { useState } from "react";
import { ShieldAlert } from "lucide-react";
import { decideApproval, type ApprovalRequest } from "@/lib/api";

interface ApprovalModalProps {
  request: ApprovalRequest | null;
  queued: number;
  onDone: (requestId: string) => void;
}

/** A model-chosen command waits here for your click (read-only commands skip this). */
export function ApprovalModal({ request, queued, onDone }: ApprovalModalProps) {
  const [busy, setBusy] = useState(false);
  if (!request) return null;

  const decide = async (approve: boolean) => {
    setBusy(true);
    try {
      await decideApproval(request.requestId, approve);
    } catch {
      // already resolved or timed out
    } finally {
      setBusy(false);
      onDone(request.requestId);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-xs p-4" data-testid="approval-modal">
      <div className="w-full max-w-lg rounded-xl border p-4 space-y-3 shadow-2xl" style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--accent-amber)" }}>
        <div className="flex items-center gap-2">
          <ShieldAlert className="w-4 h-4" style={{ color: "var(--accent-amber)" }} />
          <h3 className="text-sm font-semibold">
            The orchestrator wants to run a command on [{request.nodeId}]
            {queued > 1 ? ` · ${queued - 1} more waiting` : ""}
          </h3>
        </div>
        <pre className="rounded-lg border p-2 text-[0.75rem] font-mono-code whitespace-pre-wrap break-all max-h-64 overflow-auto" style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)" }}>
          {request.command}
        </pre>
        {request.cwd && (
          <div className="text-[0.6875rem]" style={{ color: "var(--text-muted)" }}>
            in <span className="font-mono-code">{request.cwd}</span>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button disabled={busy} onClick={() => void decide(false)} className="tap px-4 rounded-md border text-sm cursor-pointer" style={{ borderColor: "var(--border-subtle)" }}>
            Deny
          </button>
          <button disabled={busy} onClick={() => void decide(true)} className="tap px-4 rounded-md text-sm font-semibold cursor-pointer" style={{ backgroundColor: "var(--accent-amber)", color: "#111" }}>
            Run it
          </button>
        </div>
      </div>
    </div>
  );
}
