// Device colours (maintainer-approved 2026-11-03). Every card of a device shares its
// colour, so a device's two or three cards are found at a glance. The names match the
// backend's palette (`app/models/instance.py`, same order); the light and dark shades are
// the `--vt-dev-<name>` tokens in index.css. No red, amber or green: those mean problems
// and healthy, and a problem's colour always wins over the device colour on figures.

import type { Instance } from "../api";

export const DEVICE_COLORS = [
  { name: "blue", label: "Blue" },
  { name: "violet", label: "Violet" },
  { name: "teal", label: "Teal" },
  { name: "pink", label: "Pink" },
  { name: "orange", label: "Orange" },
  { name: "lime", label: "Lime" },
  { name: "cyan", label: "Cyan" },
  { name: "indigo", label: "Indigo" },
  { name: "fuchsia", label: "Fuchsia" },
  { name: "sky", label: "Sky" },
  { name: "purple", label: "Purple" },
] as const;

export type DeviceColorName = (typeof DEVICE_COLORS)[number]["name"];

const NAMES: ReadonlySet<string> = new Set(DEVICE_COLORS.map((c) => c.name));

export function isDeviceColor(name: string | null | undefined): name is DeviceColorName {
  return typeof name === "string" && NAMES.has(name);
}

/** The CSS colour of a palette name (follows light and dark mode). */
export function colorVar(name: DeviceColorName): string {
  return `var(--vt-dev-${name})`;
}

/**
 * The device's colour as a CSS value, or undefined when it has none (or an unknown name,
 * from a newer backend): then cards show no device colour.
 */
export function deviceColor(instance: Pick<Instance, "color"> | undefined): string | undefined {
  const name = instance?.color;
  return isDeviceColor(name) ? colorVar(name) : undefined;
}

/** A light tint of a colour, for the header's icon chip. */
export function tint(color: string, percent = 15): string {
  return `color-mix(in srgb, ${color} ${percent}%, transparent)`;
}
