// Value formatters for cards. All return a short display string; a missing or non-finite
// value gives "–" so a card never shows "NaN".

const DASH = "–";

const finite = (v: number | undefined | null): v is number =>
  v !== undefined && v !== null && Number.isFinite(v);

function scaled(value: number, units: string[], base: number): string {
  let v = Math.abs(value);
  let i = 0;
  while (v >= base && i < units.length - 1) {
    v /= base;
    i++;
  }
  const text = v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1);
  return `${value < 0 ? "-" : ""}${text} ${units[i]}`;
}

/** Binary units: 1536 -> "1.5 KiB". */
export function formatBytes(value: number | undefined): string {
  return finite(value) ? scaled(value, ["B", "KiB", "MiB", "GiB", "TiB", "PiB"], 1024) : DASH;
}

/** Decimal units, as network speeds are quoted: 2_500_000 -> "2.5 Mbit/s". */
export function formatBitsPerSecond(value: number | undefined): string {
  return finite(value)
    ? scaled(value, ["bit/s", "kbit/s", "Mbit/s", "Gbit/s", "Tbit/s"], 1000)
    : DASH;
}

/** `value` is already a percentage (0 to 100). */
export function formatPercent(value: number | undefined): string {
  return finite(value) ? `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}%` : DASH;
}

/** `ratio` is 0 to 1. */
export function formatRatio(ratio: number | undefined): string {
  return finite(ratio) ? formatPercent(ratio * 100) : DASH;
}

/** Takes seconds, shows milliseconds ("1.2 ms", "340 ms") or seconds from 1 s up. */
export function formatMs(seconds: number | undefined): string {
  if (!finite(seconds)) return DASH;
  const ms = seconds * 1000;
  if (Math.abs(ms) >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${ms >= 100 ? ms.toFixed(0) : ms.toFixed(1)} ms`;
}

/** "3d 4h", "5h 12m", "42s": the two largest non-zero units. */
export function formatDuration(seconds: number | undefined): string {
  if (!finite(seconds) || seconds < 0) return DASH;
  let rest = Math.floor(seconds);
  const parts: string[] = [];
  for (const [unit, size] of [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
    ["s", 1],
  ] as const) {
    const n = Math.floor(rest / size);
    rest -= n * size;
    if (n > 0 || (unit === "s" && parts.length === 0)) parts.push(`${n}${unit}`);
    if (parts.length === 2) break;
  }
  return parts.join(" ");
}

export function formatCelsius(value: number | undefined): string {
  return finite(value) ? `${value.toFixed(0)} °C` : DASH;
}
