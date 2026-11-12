import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Loads data and keeps it fresh by re-fetching every `intervalMs`, so
 * availability changes made by other customers show up without a reload.
 * Pass `enabled = false` to skip fetching (e.g. while logged out).
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      setData(await fetcherRef.current());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      await refresh();
      // Schedule after the response, so slow requests never overlap.
      if (!cancelled) timer = window.setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [enabled, intervalMs, refresh]);

  return { data, error, refresh };
}

/** Seconds left until `isoDate`, updated every second; 0 once it has passed. */
export function useSecondsUntil(isoDate: string): number {
  const remaining = () => Math.max(0, Math.ceil((new Date(isoDate).getTime() - Date.now()) / 1000));
  const [seconds, setSeconds] = useState(remaining);

  useEffect(() => {
    setSeconds(remaining());
    const timer = window.setInterval(() => setSeconds(remaining()), 1000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isoDate]);

  return seconds;
}
