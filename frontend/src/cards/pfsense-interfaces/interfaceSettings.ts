import type { Instance } from "../../api";

/** Loopback and pf's pseudo interfaces: never shown. */
export const REAL_IF = 'interface!~"lo.*|pflog.*|pfsync.*|enc.*"';

/**
 * The admin's interface settings from the pfSense instance config: display names
 * (`igc0=WAN`) and hidden interfaces. Display only; metric labels keep the OS names.
 */
export function interfaceSettings(instance: Instance | undefined): {
  label: (name: string) => string;
  hidden: (name: string) => boolean;
} {
  const strings = (key: string) => {
    const value = instance?.config[key];
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  };
  const names = new Map<string, string>();
  for (const entry of strings("interface_names")) {
    const at = entry.indexOf("=");
    if (at > 0) names.set(entry.slice(0, at), entry.slice(at + 1));
  }
  const hidden = new Set(strings("hidden_interfaces"));
  return { label: (name) => names.get(name) ?? name, hidden: (name) => hidden.has(name) };
}
