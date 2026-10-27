// Formatting of an audit entry's `changes` for display (M3.4).

/** One displayed line of an entry's `changes`: a dotted field path and its value as text. */
export interface ChangeLine {
  path: string;
  value: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `audit.diff` output for one field: exactly `{old, new}`. */
function isDiff(value: Record<string, unknown>): value is { old: unknown; new: unknown } {
  const keys = Object.keys(value);
  return keys.length === 2 && "old" in value && "new" in value;
}

/** Strings as they are (so "<changed>" reads "<changed>"), anything else as JSON. */
function formatLeaf(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value) ?? String(value);
}

/**
 * Flatten `changes` into lines: nested objects become dotted paths (`config.port`), and an
 * `{old, new}` pair becomes "old → new". Values are never altered, only formatted.
 */
export function changeLines(changes: Record<string, unknown>, prefix = ""): ChangeLine[] {
  const lines: ChangeLine[] = [];
  for (const [key, value] of Object.entries(changes)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (isPlainObject(value) && isDiff(value)) {
      lines.push({ path, value: `${formatLeaf(value.old)} → ${formatLeaf(value.new)}` });
    } else if (isPlainObject(value) && Object.keys(value).length > 0) {
      lines.push(...changeLines(value, path));
    } else {
      lines.push({ path, value: formatLeaf(value) });
    }
  }
  return lines;
}
