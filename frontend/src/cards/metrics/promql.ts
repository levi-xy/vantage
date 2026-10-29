// Building instance-scoped PromQL. Every card query goes through `scopedQuery`, so a card
// can only ever read the series of its own instance (the `vantage_instance_id` label that
// polled metrics and scraped targets both carry).

import type { QueryResult } from "../../api";

/** Escape a label value for a PromQL string literal. */
function quote(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n")}"`;
}

/** `sel('mode="idle"')` -> `{vantage_instance_id="<id>",mode="idle"}`. */
export type Selector = (extraMatchers?: string) => string;

export function selectorFor(instanceId: string): Selector {
  const scope = `vantage_instance_id=${quote(instanceId)}`;
  return (extra) => `{${scope}${extra ? `,${extra}` : ""}}`;
}

export function scopedQuery(instanceId: string, build: (sel: Selector) => string): string {
  return build(selectorFor(instanceId));
}

/** Prometheus sends values as strings; `undefined` for NaN, Inf or no value. */
export function parseValue(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) ? n : undefined;
}

/** The value of the first series of an instant result. */
export function firstValue(result: QueryResult | undefined): number | undefined {
  return parseValue(result?.series[0]?.values[0]?.[1]);
}

/** Instant result as rows, for per-disk / per-mount results. `key` is the `label`'s value. */
export function byLabel(
  result: QueryResult | undefined,
  label: string,
): { key: string; labels: Record<string, string>; value: number | undefined }[] {
  return (result?.series ?? []).map((s) => ({
    key: s.labels[label] ?? "",
    labels: s.labels,
    value: parseValue(s.values[0]?.[1]),
  }));
}

/** Points of the first series of a range result, dropping NaN. */
export function firstSeries(result: QueryResult | undefined): [number, number][] {
  const out: [number, number][] = [];
  for (const [t, raw] of result?.series[0]?.values ?? []) {
    const v = parseValue(raw);
    if (v !== undefined) out.push([t, v]);
  }
  return out;
}
