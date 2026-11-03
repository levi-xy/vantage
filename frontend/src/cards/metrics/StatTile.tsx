import { useContext } from "react";
import { StaleContext } from "./freshness";
import { Sparkline } from "./Charts";

export interface StatTileProps {
  label: string;
  /** Already formatted (see format.ts). */
  value: string;
  /** `[unix_seconds, value]` points for a sparkline under the value. */
  spark?: [number, number][];
  tone?: "ok" | "bad";
}

// A plain value is in the device's colour (`text-device`); healthy is green, and a problem
// always wins over the device colour.
const TONES = {
  ok: "text-ok-text",
  bad: "text-crit-text",
};

/** A label, a big value, and optionally a sparkline. */
export function StatTile({ label, value, spark, tone }: StatTileProps) {
  // A past value never looks good or bad as if it were current.
  const shownTone = useContext(StaleContext) ? undefined : tone;
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-xs text-slate-500 dark:text-slate-400">{label}</span>
      <span
        className={`truncate text-xl font-semibold tabular-nums ${shownTone ? TONES[shownTone] : "text-device"}`}
      >
        {value}
      </span>
      {spark && <Sparkline className="h-8" label={`${label} over the last hour`} points={spark} />}
    </div>
  );
}
