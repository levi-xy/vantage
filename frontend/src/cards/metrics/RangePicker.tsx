// The segmented 1h / 6h / 24h / 7d / 30d control above a chart: the throughput card's and the
// device page's History section's, so both look the same.

import { RANGES } from "./ranges";

export function RangePicker({
  value,
  onChange,
}: {
  /** Index into RANGES. */
  value: number;
  onChange: (i: number) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Chart range"
      className="flex w-fit rounded border border-line text-xs"
    >
      {RANGES.map((r, i) => (
        <button
          key={r.key}
          type="button"
          aria-pressed={i === value}
          onClick={() => onChange(i)}
          className={`px-2 py-0.5 first:rounded-l last:rounded-r ${i === value ? "bg-chip font-semibold text-ink" : "text-muted hover:text-ink"}`}
        >
          {r.key}
        </button>
      ))}
    </div>
  );
}
