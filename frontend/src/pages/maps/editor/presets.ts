// The model list: presets only. Choosing one copies its shape, look and port groups into the
// box, where everything stays editable; nothing in the code knows a model afterwards. The
// researched models are in presetModels.ts; the generic ones are here.

import type { MapBox, MapNas, MapPanel, MapPortGroup } from "../../../api";
import { emptyPanel, regeneratePorts } from "../panel";
import { MODEL_PRESETS } from "./presetModels";

type GroupPreset = Omit<MapPortGroup, "id">;

export interface Preset {
  id: string;
  label: string;
  /** The dropdown group; presets without one are listed under "Generic". */
  vendor?: string;
  /** Taken from a device family or from memory, not from a photo of this exact model. */
  unverified?: boolean;
  /** Case-insensitive texts the device's sysDescr contains when it is this model. */
  match?: readonly string[];
  shape: MapBox["shape"];
  look: MapPanel["look"];
  place: MapPanel["place"];
  groups: GroupPreset[];
  /** The model line to fill in when the box has none. */
  model?: string;
  nas?: MapNas;
}

const rj45 = (count: number, patch: Partial<GroupPreset> = {}): GroupPreset => ({
  kind: "rj45",
  count,
  rows: 1,
  order: "rows",
  first: 1,
  prefix: "",
  ...patch,
});
const twoRows: Partial<GroupPreset> = { rows: 2, order: "odd_top" };

const GENERIC: readonly Preset[] = [
  {
    id: "generic-4",
    label: "Generic 4-port switch",
    shape: "device",
    look: "desktop",
    place: "bottom",
    groups: [rj45(4)],
  },
  {
    id: "generic-8",
    label: "Generic 8-port switch",
    shape: "device",
    look: "desktop",
    place: "bottom",
    groups: [rj45(8)],
  },
  {
    id: "generic-16",
    label: "Generic 16-port switch",
    shape: "device",
    look: "rack",
    place: "bottom",
    groups: [rj45(16, twoRows)],
  },
  {
    id: "generic-24",
    label: "Generic 24-port switch",
    shape: "device",
    look: "rack",
    place: "bottom",
    groups: [rj45(24, twoRows)],
  },
  {
    id: "generic-nas-4",
    label: "Generic 4-bay NAS",
    shape: "nas",
    look: "flat",
    place: "bottom",
    groups: [rj45(2)],
    nas: { bays: 4, bay_disks: [] },
  },
];

export const PRESETS: readonly Preset[] = [...MODEL_PRESETS, ...GENERIC];

/** The dropdown text: the model, marked when its layout isn't verified. */
export const presetLabel = (p: Preset): string =>
  p.unverified ? `${p.label} (unverified)` : p.label;

/** Presets by dropdown group: vendors alphabetically, "Generic" last. */
export function presetGroups(): { vendor: string; presets: Preset[] }[] {
  const by = new Map<string, Preset[]>();
  for (const p of PRESETS) {
    const vendor = p.vendor ?? "Generic";
    by.set(vendor, [...(by.get(vendor) ?? []), p]);
  }
  const vendors = [...by.keys()].filter((v) => v !== "Generic").sort((a, b) => a.localeCompare(b));
  return [...vendors, "Generic"].flatMap((vendor) => {
    const presets = by.get(vendor);
    return presets ? [{ vendor, presets }] : [];
  });
}

const alnum = /[a-z0-9]/i;

/**
 * The preset a device's sysDescr names, if any: a case-insensitive substring that doesn't run
 * into more letters or digits (GS308E is not GS308EP). The longest matching text wins.
 */
export function matchPreset(sysDescr: string | undefined): Preset | undefined {
  const text = (sysDescr ?? "").toLowerCase();
  if (!text) return undefined;
  let best: { preset: Preset; length: number } | undefined;
  for (const preset of PRESETS) {
    for (const m of preset.match ?? []) {
      const needle = m.toLowerCase();
      for (let at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + 1)) {
        const closed = !alnum.test(needle.slice(-1)) || !alnum.test(text[at + needle.length] ?? "");
        if (closed && (!best || needle.length > best.length)) {
          best = { preset, length: needle.length };
        }
      }
    }
  }
  return best?.preset;
}

/**
 * The box with a preset copied in. Ports of numbers the preset keeps stay as they were
 * (captions, VLANs, ids), so cables stay attached; the box's own size, numbers and captions
 * settings are kept.
 */
export function applyPreset(box: MapBox, preset: Preset): Partial<MapBox> {
  const old = box.panel;
  const panel = regeneratePorts({
    ...emptyPanel("manual"),
    ...(old ? { size: old.size, numbers: old.numbers, captions: old.captions } : {}),
    look: preset.look,
    place: preset.place,
    groups: preset.groups.map((g, i) => ({ ...g, id: `g${i + 1}` })),
    ports: old?.ports ?? [],
  });
  return {
    shape: preset.shape,
    panel,
    ...(preset.nas ? { nas: box.nas ?? preset.nas } : {}),
    ...(preset.model && !box.model ? { model: preset.model } : {}),
  };
}
