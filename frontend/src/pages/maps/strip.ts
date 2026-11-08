// A box's port strip: which ports it shows, in what order, and where each jack sits.
// Pure, so the drawing and the cables (which anchor at a jack's edge) agree.

import type { MapBox, MapPort } from "../../api";
import { faceplate, panelBlocks } from "../../cards/switch-panel/panel";
import type { Placed } from "../../cards/switch-panel/panel";
import type { PortState } from "../../cards/snmp-ports/ports";
import type { DevicePort } from "./devicePorts";
import type { Rect } from "./geometry";
import { panelLayout, portKind } from "./panel";

export interface StripPort {
  id: string;
  kind: "rj45" | "sfp";
  /** Typed text, drawn on the jack. Empty = nothing drawn (the name is a tooltip). */
  label: string;
  /** What the tooltip says. */
  tip: string;
  state: PortState | null;
  vlans: MapPort["vlans"];
}

export const JACK_H = 14;
const RJ45_W = 16;
const SFP_W = 18;
/** Room under a jack for its VLAN bars: tall (untagged) 8 px, short (tagged) 4 px. */
export const BAR_H = 9;
const GAP = 3;
const BLOCK_GAP = 10;
export const STRIP_PAD = 12;
const MIN_SCALE = 0.3;

const NO_VLANS: MapPort["vlans"] = { untagged: null, tagged: [] };

/** The ports a box shows, from the typed list or from the device's own interfaces. */
export function stripPorts(
  box: Pick<MapBox, "panel">,
  device: readonly DevicePort[] | undefined,
): { copper: Placed<StripPort>[][]; sfp: Placed<StripPort>[] } {
  const config = box.panel;
  if (!config) return { copper: [], sfp: [] };
  const live = new Map((device ?? []).map((p) => [p.id, p] as const));
  if (config.source === "manual") {
    const typed = config.ports.map((p): StripPort => ({
      id: p.id,
      kind: portKind(config, p),
      label: p.caption,
      tip: p.caption || p.id,
      state: live.get(p.id)?.state ?? null,
      vlans: p.vlans,
    }));
    return {
      copper: panelBlocks(typed.filter((p) => p.kind === "rj45")),
      sfp: panelBlocks(typed.filter((p) => p.kind === "sfp")).flat(),
    };
  }
  // The device's interfaces; the document's ports are overrides by port id.
  const overrides = new Map(config.ports.map((p) => [p.id, p] as const));
  const plate = faceplate((device ?? []).map((p) => ({ id: p.id, speed: p.speed, device: p })));
  const make = (cage: boolean) => (placed: Placed<{ id: string; device: DevicePort }>) => {
    const { device: d } = placed.port;
    const o = overrides.get(d.id);
    return {
      ...placed,
      port: {
        id: d.id,
        kind: cage ? "sfp" : "rj45",
        label: o?.caption ?? "",
        tip: o?.caption || d.name,
        state: d.state,
        vlans: o?.vlans ?? NO_VLANS,
      } satisfies StripPort,
    };
  };
  return {
    copper: plate.copper.map((block) => block.map(make(false))),
    sfp: plate.sfp.map(make(true)),
  };
}

export interface Jack {
  port: StripPort;
  /** Relative to the strip. */
  x: number;
  y: number;
  w: number;
}

export interface StripLayout {
  jacks: Jack[];
  width: number;
  height: number;
}

/**
 * Jacks in a row, or as a front panel (blocks of 8, even ports on top, odd below; the SFP
 * cages in a block of their own). Squeezed horizontally when the box is too narrow.
 */
export function stripLayout(
  ports: ReturnType<typeof stripPorts>,
  layout: "row" | "panel",
  room: number,
): StripLayout {
  const widthOf = (p: StripPort) => (p.kind === "sfp" ? SFP_W : RJ45_W);
  const jacks: Jack[] = [];
  let x = 0;
  let rows = 1;
  if (layout === "row") {
    for (const placed of [...ports.copper.flat(), ...ports.sfp]) {
      jacks.push({ port: placed.port, x, y: 0, w: widthOf(placed.port) });
      x += widthOf(placed.port) + GAP;
    }
  } else {
    rows = 2;
    const rowH = JACK_H + BAR_H;
    for (const block of [...ports.copper, ports.sfp].filter((b) => b.length > 0)) {
      const w = widthOf(block[0]?.port as StripPort);
      for (const placed of block) {
        jacks.push({
          port: placed.port,
          x: x + (placed.column - 1) * (w + GAP),
          y: (placed.row - 1) * rowH,
          w,
        });
      }
      x += Math.ceil(block.length / 2) * (w + GAP) + BLOCK_GAP - GAP;
    }
  }
  const natural = Math.max(0, x - (layout === "row" ? GAP : BLOCK_GAP));
  const k = natural > room && natural > 0 ? Math.max(MIN_SCALE, room / natural) : 1;
  return {
    jacks: jacks.map((j) => ({ ...j, x: j.x * k, w: j.w * k })),
    width: natural * k,
    height: rows * (JACK_H + BAR_H) - 2,
  };
}

/** Where the strip sits in a box: along the bottom, inside the padding. */
export function stripOrigin(box: Pick<Rect, "h">, layout: StripLayout): { x: number; y: number } {
  return { x: STRIP_PAD, y: box.h - layout.height - 6 };
}

/** The strip of a box, with its jacks' positions in the drawing, or null without ports. */
export function boxStrip(
  box: MapBox,
  device: readonly DevicePort[] | undefined,
): { layout: StripLayout; origin: { x: number; y: number }; rects: Map<string, Rect> } | null {
  if (!box.panel) return null;
  const layout = stripLayout(
    stripPorts(box, device),
    panelLayout(box.panel),
    box.w - 2 * STRIP_PAD,
  );
  if (layout.jacks.length === 0) return null;
  const origin = stripOrigin(box, layout);
  const rects = new Map<string, Rect>();
  for (const j of layout.jacks) {
    rects.set(j.port.id, {
      x: box.x + origin.x + j.x,
      y: box.y + origin.y + j.y,
      w: j.w,
      h: JACK_H,
    });
  }
  return { layout, origin, rects };
}
