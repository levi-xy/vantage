// Times are shown in the timezone an admin configured (Settings > Time), not the browser's
// (maintainer-approved 2026-11-07). Layout loads it and calls `setTimeZone`; everything
// else formats through this module. Until it is known, and for an unusable name, UTC.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

let zone = "UTC";
let formatter: Intl.DateTimeFormat | null = null;

function makeFormatter(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function setTimeZone(name: string | undefined): void {
  let next = name || "UTC";
  try {
    formatter = makeFormatter(next);
  } catch {
    next = "UTC";
    formatter = makeFormatter(next);
  }
  zone = next;
}

export function getTimeZone(): string {
  return zone;
}

export interface Parts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

/** The calendar fields of `ms` (epoch milliseconds) in the configured zone (or `timeZone`). */
export function zoneParts(ms: number, timeZone?: string): Parts {
  const f = timeZone ? makeFormatter(timeZone) : (formatter ??= makeFormatter(zone));
  const out: Record<string, number> = {};
  for (const p of f.formatToParts(new Date(ms))) {
    if (p.type !== "literal") out[p.type] = Number(p.value);
  }
  return {
    year: out.year ?? 0,
    month: out.month ?? 1,
    day: out.day ?? 1,
    hour: out.hour ?? 0,
    minute: out.minute ?? 0,
    second: out.second ?? 0,
  };
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "14:05", or "14:05:03" with seconds. */
export function formatClock(ms: number, withSeconds = false, timeZone?: string): string {
  const p = zoneParts(ms, timeZone);
  const hm = `${pad(p.hour)}:${pad(p.minute)}`;
  return withSeconds ? `${hm}:${pad(p.second)}` : hm;
}

/** "2026-11-07 14:05:03". */
export function formatDateTime(ms: number, timeZone?: string): string {
  const p = zoneParts(ms, timeZone);
  return `${p.year}-${pad(p.month)}-${pad(p.day)} ${formatClock(ms, true, timeZone)}`;
}

/** "7 Nov". */
export function formatDayMonth(ms: number): string {
  const p = zoneParts(ms);
  return `${p.day} ${MONTHS[p.month - 1]}`;
}

/** True when both instants fall on the same calendar day in the configured zone. */
export function sameDay(a: number, b: number): boolean {
  const x = zoneParts(a);
  const y = zoneParts(b);
  return x.year === y.year && x.month === y.month && x.day === y.day;
}

/** "14:05", with the day ("7 Nov 14:05") when `ms` isn't on the same day as `nowMs`. */
export function formatClockOrDay(ms: number, nowMs: number): string {
  const time = formatClock(ms);
  return sameDay(ms, nowMs) ? time : `${formatDayMonth(ms)} ${time}`;
}
