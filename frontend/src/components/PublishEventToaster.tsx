import { useEffect } from "react";

import { useToast } from "@/components/ui/Toast";
import { bridge, whenBridgeReady } from "@/lib/bridge";

// How often to pick up completed background (auto-publish) posts.
const POLL_MS = 20000;

/**
 * Polls the backend for completed background posts (the opt-in auto-publish loop
 * runs in Python and is otherwise invisible) and toasts each one. Rendered once,
 * app-level, inside the ToastProvider so notifications show on any tab.
 *
 * Manual posts are toasted by their own flow and are NOT recorded as events, so
 * there's no double-toast here.
 */
export function PublishEventToaster() {
  const { pushToast } = useToast();

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const events = await bridge.drainPublishEvents();
        if (cancelled) return;
        for (const event of events) {
          if (event.status !== "posted") continue;
          const when = event.at
            ? new Date(event.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
            : "";
          const label = event.account_name ?? "Account";
          const which = event.item_id ?? event.job_id;
          pushToast(`✅ ${label} — auto-posted #${which}${when ? ` at ${when}` : ""}`, "success");
        }
      } catch {
        // Non-fatal; try again next tick.
      }
    };

    void whenBridgeReady().then((ready) => {
      if (cancelled || !ready) return;
      void poll(); // catch anything that fired before mount
      timer = window.setInterval(poll, POLL_MS);
    });

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [pushToast]);

  return null;
}
