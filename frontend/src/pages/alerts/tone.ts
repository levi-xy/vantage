// Class strings and helpers the Alerts page's sections share. Only problems get a colour.

import { useEffect, useState } from "react";
import { buttonSecondary } from "../../components/styles";

/** A panel like a dashboard card, holding rows divided by a hairline. */
export const panel = "rounded-xl border border-line bg-panel";
export const row = "border-t border-line px-4 py-3 first:border-t-0";
export const buttonDanger = `${buttonSecondary} border-crit text-crit-text`;

export type Tone = "crit" | "warn" | "plain" | "muted";
const TONE: Record<Tone, string> = {
  crit: "text-crit-text",
  warn: "text-warn",
  plain: "text-ink",
  muted: "text-muted",
};
export const toneText = (tone: Tone): string => TONE[tone];

/** Now in epoch milliseconds, updated every 30 s (for "12 min"). */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}
