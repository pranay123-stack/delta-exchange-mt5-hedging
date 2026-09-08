import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";

interface QueryState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * Poll a GET endpoint.
 *
 * Polling rather than pushing everything over the WebSocket is deliberate:
 * risk and P&L are derived views that are cheap to recompute on demand and
 * expensive to keep consistent as a stream. Prices, which change constantly,
 * do come over the socket.
 */
export function useQuery<T>(
  path: string | null,
  intervalMs = 0,
): QueryState<T> & { refresh: () => void } {
  const [state, setState] = useState<QueryState<T>>({
    data: null,
    error: null,
    loading: true,
  });
  const mounted = useRef(true);

  const load = useCallback(async () => {
    if (!path) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    try {
      const data = await api.get<T>(path);
      if (mounted.current) setState({ data, error: null, loading: false });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : String(error);
      if (mounted.current)
        setState((prev) => ({ ...prev, error: message, loading: false }));
    }
  }, [path]);

  useEffect(() => {
    mounted.current = true;
    void load();
    if (intervalMs > 0) {
      const timer = window.setInterval(() => void load(), intervalMs);
      return () => {
        mounted.current = false;
        window.clearInterval(timer);
      };
    }
    return () => {
      mounted.current = false;
    };
  }, [load, intervalMs]);

  return { ...state, refresh: () => void load() };
}

/**
 * Run a mutating call, tracking in-flight state and the last error.
 *
 * `run` is generic per *call* rather than per hook instance, so a component
 * that fires several different mutations gets the right result type from each
 * without declaring a union up front.
 */
export function useAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async <R,>(fn: () => Promise<R>): Promise<R | null> => {
    setPending(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String(err);
      setError(message);
      return null;
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error, setError };
}
