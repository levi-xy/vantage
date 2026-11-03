// The pfSense interfaces the Gateways card watches: how a choice is stored in the card's
// settings, and the rows the card shows. Pure functions, so they're easy to test.

import type { QueryResult } from "../../api";
import { byLabel } from "../metrics/promql";

export const MAX_INTERFACES = 8;
const SETTING_KEYS = Array.from({ length: MAX_INTERFACES }, (_, i) => `interface${i + 1}`);

type Settings = Record<string, string | number | boolean | null>;

/** The interfaces (OS names, as Telegraf's `interface` label has them) chosen, in order. */
export function interfacesFromSettings(settings: Record<string, unknown>): string[] {
  const out: string[] = [];
  for (const key of SETTING_KEYS) {
    const v = settings[key];
    if (typeof v === "string" && v !== "" && !out.includes(v)) out.push(v);
  }
  return out;
}

/** Card settings holding `interfaces` (at most MAX_INTERFACES), replacing any earlier choice. */
export function settingsWithInterfaces(settings: Settings, interfaces: string[]): Settings {
  const rest = Object.fromEntries(
    Object.entries(settings).filter(([k]) => !SETTING_KEYS.includes(k)),
  );
  interfaces.slice(0, MAX_INTERFACES).forEach((iface, i) => (rest[`interface${i + 1}`] = iface));
  return rest;
}

export interface GatewayRow {
  /** The OS name. */
  id: string;
  /** The display name (`interface_names`, pfSense's description, else the OS name). */
  name: string;
  rx: number | undefined;
  tx: number | undefined;
  /** Its counters are missing from the latest scrape, or the device's data is stale. */
  noData: boolean;
}

/**
 * One row per chosen interface, in the order chosen. `present` is the raw counter query
 * (an interface Telegraf no longer reports drops out of it at once); `stale`: the device's
 * data is old, so no row can be trusted.
 */
export function gatewayRows(
  chosen: string[],
  present: QueryResult | undefined,
  rx: QueryResult | undefined,
  tx: QueryResult | undefined,
  label: (iface: string) => string,
  stale: boolean,
): GatewayRow[] {
  const seen = new Set(byLabel(present, "interface").map((r) => r.key));
  const rxBy = new Map(byLabel(rx, "interface").map((r) => [r.key, r.value] as const));
  const txBy = new Map(byLabel(tx, "interface").map((r) => [r.key, r.value] as const));
  return chosen.map((id) => {
    const noData = stale || !seen.has(id);
    return {
      id,
      name: label(id),
      rx: noData ? undefined : rxBy.get(id),
      tx: noData ? undefined : txBy.get(id),
      noData,
    };
  });
}

/** Problems first (in chosen order), then the rest: a problem is never the row cut off. */
export const problemsFirst = (rows: GatewayRow[]): GatewayRow[] =>
  [...rows].sort((a, b) => Number(b.noData) - Number(a.noData));
