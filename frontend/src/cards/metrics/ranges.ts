// The chart windows the range picker offers.

/** The selectable chart windows. */
export const RANGES = [
  { key: "1h", seconds: 3600, text: "1 hour" },
  { key: "6h", seconds: 6 * 3600, text: "6 hours" },
  { key: "24h", seconds: 24 * 3600, text: "24 hours" },
  { key: "7d", seconds: 7 * 86400, text: "7 days" },
  { key: "30d", seconds: 30 * 86400, text: "30 days" },
] as const;
