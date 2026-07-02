import { useEffect, useRef } from "react";

/**
 * Re-run `fn` on an interval and whenever the window regains focus or becomes
 * visible again, so dashboard-style screens stay fresh without the user
 * tab-hopping to force a remount. Data reads are local (SQLite via the
 * pywebview bridge), so a modest interval is cheap.
 */
export function useAutoRefresh(
  fn: () => void | Promise<void>,
  intervalMs = 30000,
  enabled = true,
) {
  // Latest callback without resetting the interval on every render.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => void fnRef.current();
    const id = window.setInterval(tick, intervalMs);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") tick();
    };
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs, enabled]);
}
