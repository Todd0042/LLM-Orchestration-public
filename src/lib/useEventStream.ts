import { useEffect, useRef, useState } from "react";
import { eventStreamUrl } from "@/lib/api";

export type OrchEvent = { type: string; [key: string]: unknown };

/**
 * One live connection to /ws/events for the whole app.
 * The handler is read through a ref, so changing chats or state never reconnects the socket;
 * dropped connections retry with backoff using a fresh one-time ticket.
 */
export function useEventStream(enabled: boolean, onEvent: (ev: OrchEvent) => void): { connected: boolean } {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let alive = true;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const connect = async () => {
      if (!alive) return;
      try {
        ws = new WebSocket(await eventStreamUrl());
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        retry = 0;
        setConnected(true);
      };
      ws.onmessage = (e) => {
        try {
          handler.current(JSON.parse(e.data) as OrchEvent);
        } catch {
          // ignore malformed events
        }
      };
      ws.onclose = () => {
        setConnected(false);
        schedule();
      };
    };

    const schedule = () => {
      if (!alive) return;
      const delay = Math.min(30_000, 1000 * 2 ** retry++);
      timer = setTimeout(() => void connect(), delay);
    };

    void connect();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, [enabled]);

  return { connected };
}
