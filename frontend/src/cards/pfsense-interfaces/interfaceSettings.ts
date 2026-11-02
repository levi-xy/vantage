import type { Instance } from "../../api";

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
 * The admin's interface settings from the pfSense instance config: display names
 * (`igc0=WAN`) and hidden interfaces. Display only; metric labels keep the OS names.
 */
export function interfaceSettings(instance: Instance | undefined): {
  label: (name: string) => string;
  hidden: (name: string) => boolean;
} {
  const hidden = new Set(strings(instance, "hidden_interfaces"));
  return { label: displayNames(instance, "interface_names"), hidden: (name) => hidden.has(name) };
}

/**
 * Gateway display names from the pfSense instance config (`192.0.2.1=WAN_DHCP`), keyed by
 * the ping target (`url` label). Display only; the label keeps the address.
 */
export function gatewayLabel(instance: Instance | undefined): (url: string) => string {
  return displayNames(instance, "gateway_names");
}
