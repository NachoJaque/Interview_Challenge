import { useCallback, useEffect, useRef, useState } from "react";
import { api, type PurchaseRules } from "./api";

const MAX_POLL_DELAY_MS = 30_000;

/**
 * How long to wait before the next poll. After failures the wait doubles (up
 * to a ceiling), so a struggling server is given room instead of more load.
 * `jitter` (0..1) spreads clients out so they do not all return at once.
 */
export function nextPollDelay(intervalMs: number, consecutiveFailures: number, jitter: number): number {
  const backedOff = Math.min(intervalMs * 2 ** consecutiveFailures, MAX_POLL_DELAY_MS);
  return Math.round(backedOff * (1 + 0.2 * jitter));
}

/**
 * Loads data and keeps it fresh by re-fetching every `intervalMs`, so
 * availability changes made by other customers show up without a reload.
 * Pass `enabled = false` to skip fetching (e.g. while logged out).
 *
 * It is careful with the server: it pauses while the tab is hidden (and
 * refreshes at once when the tab comes back), and it slows down after errors.
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  /** Resolves to whether the load succeeded. */
  const refresh = useCallback(async (): Promise<boolean> => {
    try {
      setData(await fetcherRef.current());
      setError(null);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
      return false;
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let failures = 0;

    const tick = async () => {
      failures = (await refresh()) ? 0 : failures + 1;
      // Schedule after the response, so slow requests never overlap.
      if (!cancelled && !document.hidden) {
        timer = window.setTimeout(tick, nextPollDelay(intervalMs, failures, Math.random()));
      }
    };
    const onVisibilityChange = () => {
      window.clearTimeout(timer);
      if (!document.hidden) void tick();
    };

    void tick();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [enabled, intervalMs, refresh]);

  return { data, error, refresh };
}

/**
 * The purchase limits, loaded once from the API. Null until they arrive (or
 * if they cannot be loaded): the API enforces them either way.
 */
export function usePurchaseRules(): PurchaseRules | null {
  const [rules, setRules] = useState<PurchaseRules | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .purchaseRules()
      .then((loaded) => {
        if (!cancelled) setRules(loaded);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  return rules;
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
