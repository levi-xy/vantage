import { TimeSeriesChart } from "./TimeSeriesChart";

export interface StatTileProps {
  label: string;
  /** Already formatted (see format.ts). */
  value: string;
  /** `[unix_seconds, value]` points for a sparkline under the value. */
  spark?: [number, number][];
  tone?: "ok" | "bad";
}

const TONES = {
  ok: "text-emerald-700 dark:text-emerald-400",
  bad: "text-red-700 dark:text-red-400",
};

/** A label, a big value, and optionally a sparkline. */
export function StatTile({ label, value, spark, tone }: StatTileProps) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-xs text-slate-500 dark:text-slate-400">{label}</span>
      <span className={`text-xl font-semibold tabular-nums ${tone ? TONES[tone] : ""}`}>
        {value}
      </span>
      {spark && spark.length > 1 && (
        <div className="h-8">
          <TimeSeriesChart
            sparkline
            label={`${label} over the last hour`}
            series={[{ name: label, points: spark }]}
          />
        </div>
      )}
    </div>
  );
}
