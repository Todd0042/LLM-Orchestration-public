import { useEffect, useRef, useState } from "react";
import { ticketedWsUrl } from "@/lib/api";
import type { SlotId } from "@/lib/orchTerminal";

type Conn = "connecting" | "live" | "reconnecting" | "ended";

/**
 * One live view of an agent's tmux session. xterm.js is loaded only when a terminal is shown, so the
 * rest of the app stays light. `canType` is a hint for the UI: the server decides what reaches the
 * agent (only the baton holder, never over the internet) and drops everything else.
 */
export function TerminalPane({ cardId, slot, canType, label }: { cardId: string; slot: SlotId; canType: boolean; label: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [conn, setConn] = useState<Conn>("connecting");
  const canTypeRef = useRef(canType);
  canTypeRef.current = canType;

  useEffect(() => {
    let disposed = false;
    let ws: WebSocket | null = null;
    let retry = 0;
    let retryTimer = 0;
    const cleanups: Array<() => void> = [];

    (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit"), import("@xterm/xterm/css/xterm.css")]);
      if (disposed || !host.current) return;
      const css = getComputedStyle(document.documentElement);
      const term = new Terminal({
        cursorBlink: true,
        convertEol: false,
        scrollback: 5000,
        fontFamily: "ui-monospace, 'JetBrains Mono', 'Fira Code', Menlo, monospace",
        fontSize: 13,
        theme: { background: css.getPropertyValue("--bg-canvas").trim() || "#0b0f14", foreground: "#e6edf3" },
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(host.current);
      cleanups.push(() => term.dispose());
      // xterm.js 6.0.0's mode query (DECRQM, "ESC[?2026$p", sent by tmux 3.8 on attach) throws a
      // ReferenceError that stops all drawing. Answer nothing instead: tmux then assumes the mode isn't
      // supported. Remove once a stable xterm.js release fixes requestMode.
      for (const prefix of ["?", ""]) {
        const hook = term.parser.registerCsiHandler({ prefix, intermediates: "$", final: "p" }, () => true);
        cleanups.push(() => hook.dispose());
      }

      const sendSize = () => {
        try {
          fit.fit();
        } catch {
          return;
        }
        if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "r", c: term.cols, r: term.rows }));
      };
      const ro = new ResizeObserver(() => sendSize());
      ro.observe(host.current);
      cleanups.push(() => ro.disconnect());
      const typed = term.onData((d) => {
        if (canTypeRef.current && ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d }));
      });
      cleanups.push(() => typed.dispose());

      const connect = async () => {
        if (disposed) return;
        try {
          ws = new WebSocket(await ticketedWsUrl(`/ws/orch-terminal/${encodeURIComponent(cardId)}/${slot}`));
        } catch {
          scheduleRetry();
          return;
        }
        ws.binaryType = "arraybuffer";
        ws.onopen = () => {
          retry = 0;
          setConn("live");
          term.reset();
          sendSize();
        };
        ws.onmessage = (ev) => term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data as ArrayBuffer));
        ws.onclose = (ev) => {
          if (disposed) return;
          if (ev.code === 1008) {
            setConn("ended"); // refused: the session is gone or the sign-in expired
            return;
          }
          scheduleRetry();
        };
      };
      // The server restarting (or the network blinking) must not lose the view: the agent keeps running in tmux.
      const scheduleRetry = () => {
        setConn("reconnecting");
        retry = Math.min(retry + 1, 6);
        retryTimer = window.setTimeout(() => void connect(), 500 * 2 ** retry);
      };
      cleanups.push(() => {
        window.clearTimeout(retryTimer);
        ws?.close();
      });
      void connect();
    })();

    return () => {
      disposed = true;
      cleanups.reverse().forEach((f) => f());
    };
  }, [cardId, slot]);

  return (
    <div className="relative h-full w-full min-h-0" data-testid={`terminal-${slot}`}>
      <div ref={host} className="absolute inset-0 p-1" aria-label={`${label} terminal`} />
      {conn !== "live" && (
        <div className="absolute top-2 right-2 rounded px-2 py-0.5 text-xs" style={{ backgroundColor: "var(--bg-elevated)", color: "var(--text-muted)" }}>
          {conn === "connecting" ? "Connecting…" : conn === "reconnecting" ? "Reconnecting…" : "Terminal closed"}
        </div>
      )}
    </div>
  );
}
