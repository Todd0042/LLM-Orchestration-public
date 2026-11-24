import { useEffect, useState } from "react";
import { KeyRound, X } from "lucide-react";
import type { PasswordPromptEvent } from "@/types/orch";
import { submitSudoPassword } from "@/lib/api";

interface SudoPasswordModalProps {
  promptEvent: PasswordPromptEvent | null;
  onClose: () => void;
}

export function SudoPasswordModal({ promptEvent, onClose }: SudoPasswordModalProps) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPw("");
  }, [promptEvent?.requestId]);

  if (!promptEvent) return null;

  const send = async (password: string | null) => {
    setBusy(true);
    try {
      await submitSudoPassword({
        requestId: promptEvent.requestId,
        nodeId: promptEvent.nodeId,
        password,
      });
      setPw("");
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-xs p-4">
      <div
        className="w-full max-w-md rounded-xl border p-4 space-y-3 shadow-2xl"
        style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--accent-amber)" }}
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <KeyRound className="w-4 h-4" style={{ color: "var(--accent-amber)" }} />
            <h3 className="text-sm font-semibold">
              Password Prompt · Node [{promptEvent.nodeId}]
            </h3>
          </div>
          <button
            onClick={() => void send(null)}
            className="p-1 rounded hover:opacity-80 cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div
          className="rounded-lg border p-2 text-[0.6875rem] font-mono-code break-all"
          style={{ backgroundColor: "var(--bg-muted)", borderColor: "var(--border-subtle)", color: "var(--text-secondary)" }}
        >
          {promptEvent.command}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (pw) void send(pw);
          }}
          className="space-y-3"
        >
          <div className="space-y-1">
            <label className="text-xs font-medium">{promptEvent.prompt}</label>
            <input
              type="password"
              autoFocus
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              placeholder="Enter password (memory-only, never stored)…"
              className="w-full rounded-lg border px-3 py-2 text-xs font-mono-code outline-none"
              style={{ backgroundColor: "var(--bg-elevated)", borderColor: "var(--border-strong)" }}
            />
          </div>

          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void send(null)}
              className="px-3 py-1.5 rounded-md border text-xs cursor-pointer"
              style={{ borderColor: "var(--border-subtle)" }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !pw}
              className="px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer disabled:opacity-40"
              style={{ backgroundColor: "var(--accent-amber)", color: "#0b0e14" }}
            >
              Submit Password
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
