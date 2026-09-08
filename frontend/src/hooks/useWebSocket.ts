import { useEffect, useRef, useState } from "react";

export interface Frame {
  topic: string;
  ts?: string;
  data: Record<string, unknown>;
}

/**
 * Subscribe to the backend's live feed.
 *
 * Reconnects with a capped backoff. The latest frame per topic is kept rather
 * than a growing log, because every consumer here renders current state.
 */
export function useWebSocket(topics: string[]) {
  const [connected, setConnected] = useState(false);
  const [frames, setFrames] = useState<Record<string, Frame>>({});
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const key = topics.join(",");

  useEffect(() => {
    let closed = false;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${protocol}://${window.location.host}/ws?topics=${key}`;
      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        attemptRef.current = 0;
        setConnected(true);
      };
      socket.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data) as Frame;
          setFrames((prev) => ({ ...prev, [frame.topic]: frame }));
        } catch {
          // A malformed frame must not tear down the connection.
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        attemptRef.current = Math.min(attemptRef.current + 1, 6);
        timer = window.setTimeout(connect, 500 * 2 ** (attemptRef.current - 1));
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
    };
  }, [key]);

  return { connected, frames };
}
