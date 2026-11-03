import type { Instance, QueryResult } from "../../api";

/** Loopback and pf's pseudo interfaces: never shown. */
export const REAL_IF = 'interface!~"lo.*|pflog.*|pfsync.*|enc.*"';

function strings(instance: Instance | undefined, key: string): string[] {
  // `config` is always there on an instance from the API; test fixtures may leave it out.
  const value = (instance?.config as Record<string, unknown> | undefined)?.[key];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** `key=name` entries of a pfSense config list, as a lookup that falls back to the key. */
function displayNames(instance: Instance | undefined, key: string): (id: string) => string {
  const names = new Map<string, string>();
  for (const entry of strings(instance, key)) {
    const at = entry.indexOf("=");
    if (at > 0) names.set(entry.slice(0, at), entry.slice(at + 1));
  }
  return (id) => names.get(id) ?? id;
}

/**
 * The interface descriptions set on pfSense itself (WAN, LAN, IoT), from the optional
 * `pfsense_interface_info{interface, descr}` series a small script on pfSense feeds to
 * Telegraf (docs/integrations/pfsense.md). Missing when the script isn't installed.
 */
export const IF_INFO_METRIC = "pfsense_interface_info";

/** Longest description shown; the rest is cut. */
const MAX_DESCR = 32;

/** Interface name to its pfSense description, per `vantage_instance_id:interface`. */
export function interfaceDescriptions(result: QueryResult | undefined): Map<string, string> {
  const out = new Map<string, string>();
  for (const s of result?.series ?? []) {
    const { vantage_instance_id: id, interface: iface, descr } = s.labels;
    const text = descr?.trim().slice(0, MAX_DESCR);
    if (id && iface && text) out.set(`${id}:${iface}`, text);
  }
  return out;
}

/**
 * The admin's interface settings: display names and hidden interfaces. A name set in the
 * Vantage device settings (`igc0=WAN`) wins over the description set on pfSense, which wins
 * over the OS name. Display only; metric labels keep the OS names.
 */
export function interfaceSettings(
  instance: Instance | undefined,
  descriptions?: Map<string, string>,
): {
  label: (name: string) => string;
  hidden: (name: string) => boolean;
} {
  const hidden = new Set(strings(instance, "hidden_interfaces"));
  const names = displayNames(instance, "interface_names");
  return {
    label: (name) => {
      const set = names(name);
      if (set !== name) return set;
      return descriptions?.get(`${instance?.id ?? ""}:${name}`) ?? name;
    },
    hidden: (name) => hidden.has(name),
  };
}
