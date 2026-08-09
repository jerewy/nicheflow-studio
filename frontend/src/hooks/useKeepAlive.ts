import { useEffect, useRef, type RefObject } from "react";

/**
 * Tab screens are kept mounted (CSS-hidden via the `hidden` attribute) so
 * background jobs and their progress state survive tab switches. This hook
 * restores the data freshness a remount used to provide: `fn` runs each time
 * `active` flips back to true (the tab being revisited).
 */
export function useRevisitRefresh(active: boolean, fn: () => void) {
  // Latest callback without re-running the revisit effect on every render.
  // Declared first so it runs before the revisit effect in each commit.
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });
  const wasActive = useRef(active);

  useEffect(() => {
    if (active && !wasActive.current) fnRef.current();
    wasActive.current = active;
  }, [active]);
}

/**
 * display:none does not stop <video>/<audio> playback, so a kept-alive tab
 * would keep playing sound while hidden. Pause any media inside [hidden]
 * wrappers under `ref` whenever the visible tab (`key`) changes.
 */
export function usePauseHiddenMedia(ref: RefObject<HTMLElement | null>, key: unknown) {
  useEffect(() => {
    ref.current
      ?.querySelectorAll<HTMLMediaElement>("[hidden] video, [hidden] audio")
      .forEach((media) => media.pause());
  }, [ref, key]);
}
