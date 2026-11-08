import { useSyncExternalStore } from "react";

/** Tailwind's `md` breakpoint: from here the map is drawn, below it the phone list. */
export const WIDE_QUERY = "(min-width: 768px)";

/** Whether a media query matches, kept up to date. Without matchMedia (jsdom) it's `fallback`. */
export function useMediaQuery(query: string, fallback = true): boolean {
  const subscribe = (notify: () => void) => {
    if (typeof window.matchMedia !== "function") return () => undefined;
    const mq = window.matchMedia(query);
    mq.addEventListener("change", notify);
    return () => mq.removeEventListener("change", notify);
  };
  const snapshot = () =>
    typeof window.matchMedia === "function" ? window.matchMedia(query).matches : fallback;
  return useSyncExternalStore(subscribe, snapshot, () => fallback);
}
