// The model list: presets only. Choosing one copies its shape, look and port groups into the
// box, where everything stays editable; nothing in the code knows a model afterwards. The
// names are the only place a vendor appears.

import type { MapBox, MapNas, MapPanel, MapPortGroup } from "../../../api";
import { emptyPanel, regeneratePorts } from "../panel";

type GroupPreset = Omit<MapPortGroup, "id">;

export interface Preset {
  id: string;
  label: string;
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
const sfpPlus = (count: number, patch: Partial<GroupPreset> = {}): GroupPreset =>
  rj45(count, { kind: "sfp_plus", ...patch });
const twoRows: Partial<GroupPreset> = { rows: 2, order: "odd_top" };

export const PRESETS: readonly Preset[] = [
  {
    id: "crs326-24g-2s",
    label: "MikroTik CRS326-24G-2S+",
    model: "CRS326-24G-2S+",
    shape: "device",
    look: "rack",
    place: "side",
    groups: [rj45(24, { ...twoRows, prefix: "ether" }), sfpPlus(2, { prefix: "sfp-sfpplus" })],
  },
  {
    id: "crs328-24p-4s",
    label: "MikroTik CRS328-24P-4S+",
    model: "CRS328-24P-4S+",
    shape: "device",
    look: "rack",
    place: "side",
    groups: [rj45(24, { ...twoRows, prefix: "ether" }), sfpPlus(4, { prefix: "sfp-sfpplus" })],
  },
  {
    id: "crs310-8g-2s",
    label: "MikroTik CRS310-8G+2S+",
    model: "CRS310-8G+2S+",
    shape: "device",
    look: "desktop",
    place: "bottom",
    groups: [rj45(8, { prefix: "ether" }), sfpPlus(2, { prefix: "sfp-sfpplus" })],
  },
  {
    id: "css610-8g-2s",
    label: "MikroTik CSS610-8G-2S+",
    model: "CSS610-8G-2S+",
    shape: "device",
    look: "desktop",
    place: "bottom",
    groups: [rj45(8, { prefix: "port" }), sfpPlus(2, { first: 9, prefix: "sfpplus" })],
  },
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
