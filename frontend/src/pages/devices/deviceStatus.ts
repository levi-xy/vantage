// How the device pages show a device's state. Same words and colours as the card header:
// "UP" is green; a problem is critical (DOWN) or amber (a device failing polls that isn't
// down yet, on its problem line).

import type { Instance, InstanceStatus } from "../../api";
import { ageText, clockTime } from "../../cards/problems";
import { formatDateTime } from "../../app/time";

type State = InstanceStatus["state"];

export type Tone = "ok" | "crit" | "warn" | undefined;

export interface DeviceHealth {
  /** "UP", "DOWN", "PENDING", "UNKNOWN", "DISABLED". */
  text: string;
  /** Colour of the state word. */
  tone: Tone;
  /** The short problem line (the collector's error), or null when there is none. */
  problem: string | null;
  /** Colour of the problem line. */
  problemTone: Tone;
  /** Sort order: 0 down, 1 failing, 2 the rest, 3 disabled. */
  rank: number;
  /** The collector doesn't poll it; the state is Prometheus' scrape `up`. */
  scraped: boolean;
}

/** `shown` is `shownState(instance, scrape)`: the backend's state or Prometheus' scrape state. */
export function deviceHealth(instance: Instance, shown: State): DeviceHealth {
  const scraped = instance.enabled && instance.status.state === "unknown";
  const error = instance.status.error;
  if (!instance.enabled) {
    return {
      text: "DISABLED",
      tone: undefined,
      problem: null,
      problemTone: undefined,
      rank: 3,
      scraped,
    };
  }
  if (shown === "down") {
    return {
      text: "DOWN",
      tone: "crit",
      problem: error ?? (scraped ? "Prometheus can't scrape it" : null),
      problemTone: "crit",
      rank: 0,
      scraped,
    };
  }
  return {
    text: shown.toUpperCase(),
    tone: shown === "up" ? "ok" : undefined,
    problem: error,
    problemTone: error ? "warn" : undefined,
    rank: error ? 1 : 2,
    scraped,
  };
}

export const TONE_CLASS: Record<"ok" | "crit" | "warn", string> = {
  ok: "font-semibold text-ok-text",
  crit: "font-semibold text-crit-text",
  warn: "text-warn",
};

/** "14:03 · 12 min ago", or null. The full local time goes in `full` for a tooltip. */
export function timeText(
  iso: string | null,
  nowMs: number,
): { short: string; full: string } | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return {
    short: `${clockTime(ms, nowMs)} · ${ageText(nowMs - ms)} ago`,
    full: formatDateTime(ms),
  };
}
