// Where every jack of a box's front panel sits. Pure, so the drawing and the cables (which
// meet a jack's edge) agree: the drawing places each piece at the numbers computed here, and
// the cables add the panel's measured origin in the box.

import type { MapBox, MapPanel, MapPort, MapPortGroup, MapPortKind } from "../../api";
import type { DevicePort } from "./devicePorts";
import type { Rect } from "./geometry";
import { isCage, portNumber, resolvePanel } from "./panel";

export type JackShape = "rj45" | "cage" | "console" | "usb";

export const jackShape = (kind: MapPortKind): JackShape =>
  isCage(kind) ? "cage" : kind === "console" ? "console" : kind === "usb" ? "usb" : "rj45";

/** Jack drawing sizes at "m", in px: width, height (the `viewBox` of each drawing). */
export const JACK_BOX: Record<JackShape, readonly [number, number]> = {
  rj45: [26, 22],
  cage: [34, 18],
  console: [26, 16],
  usb: [18, 12],
};
/** QSFP cages are wider than the other cages. */
const QSFP_BOX: readonly [number, number] = [40, 20];
export const jackBox = (kind: MapPortKind): readonly [number, number] =>
  kind === "qsfp" ? QSFP_BOX : JACK_BOX[jackShape(kind)];

const SCALE = { s: 0.78, m: 1, l: 1.3 } as const;
const NUMBER_H = 11;
const CAPTION_H = 11;
const BARS_H = 9;
const GAP = 2;
const CELL_PAD = 8;
const ROW_GAP = 4;
const GROUP_GAP = 14;
const MIN_SQUEEZE = 0.45;

/** The inset strip around the panel: padding plus the 1 px border. */
export const INSET = 9;
/** The side padding of a box's content, by shape and look (the panel lines up with it). */
export const boxPadX = (box: Pick<MapBox, "shape" | "panel">): number =>
  box.shape === "ap" || (box.shape === "device" && box.panel?.look === "rack") ? 20 : 14;
/** Where a box's content starts below its top edge (room for the name tab). */
const CONTENT_TOP = 24;

export interface Jack {
  port: MapPort;
  group: MapPortGroup;
  /** The number shown (group's first number + place). */
  number: number;
  shape: JackShape;
  /** Drawn upside down (top row of an `odd_top` group): number below, caption above. */
  flip: boolean;
  /** The cell that holds the jack and its texts, relative to the panel. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** Where each piece sits inside the cell. */
  jackX: number;
  jackY: number;
  jackW: number;
  jackH: number;
  numberY: number;
  barsY: number;
  captionY: number;
}

export interface PanelFlags {
  numbers: boolean;
  captions: boolean;
  bars: boolean;
}

export interface PanelGeometry {
  jacks: Jack[];
  width: number;
  height: number;
  flags: PanelFlags;
}

/**
 * The jacks of a panel in a row of groups, left to right. Within a two-row group, an
 * `odd_top` group numbers its columns in pairs (odd on top), another fills the top row first.
 * Squeezed horizontally when `room` is too narrow.
 */
export function panelGeometry(
  panel: MapPanel,
  device: readonly DevicePort[] | undefined,
  room: number,
): PanelGeometry | null {
  const { groups, ports } = resolvePanel(panel, device);
  const shown = groups
    .map((group) => ({
      group,
      list: ports.filter((p) => p.group === group.id && !p.hidden).sort((a, b) => a.n - b.n),
    }))
    .filter((g) => g.list.length > 0);
  if (shown.length === 0) return null;

  const f = SCALE[panel.size];
  const flags: PanelFlags = {
    numbers: panel.numbers,
    captions: panel.captions !== "off",
    bars: shown.some((g) => g.list.some((p) => p.vlans.untagged || p.vlans.tagged.length > 0)),
  };
  const dims = (kind: MapPortKind) => {
    const [w, h] = jackBox(kind);
    return [Math.round(w * f * 2) / 2, Math.round(h * f * 2) / 2] as const;
  };
  const slotH = Math.max(...shown.map((g) => dims(g.group.kind)[1]));
  const parts = 1 + Number(flags.numbers) + Number(flags.captions) + Number(flags.bars);
  const cellH =
    slotH +
    (flags.numbers ? NUMBER_H : 0) +
    (flags.captions ? CAPTION_H : 0) +
    (flags.bars ? BARS_H : 0) +
    GAP * (parts - 1);
  const rowsOf = (g: { group: MapPortGroup; list: MapPort[] }) =>
    Math.min(g.group.rows, g.list.length);
  const maxRows = Math.max(...shown.map(rowsOf));
  const height = maxRows * cellH + (maxRows - 1) * ROW_GAP;

  let x = 0;
  const jacks: Jack[] = [];
  for (const g of shown) {
    const rows = rowsOf(g);
    const cols = Math.ceil(g.list.length / rows);
    const [jw, jh] = dims(g.group.kind);
    const cellW = jw + CELL_PAD;
    const offset = ((maxRows - rows) * (cellH + ROW_GAP)) / 2;
    g.list.forEach((port, k) => {
      let col = k;
      let row = 0;
      if (rows === 2 && g.group.odd_top) [col, row] = [Math.floor(k / 2), k % 2];
      else if (rows === 2) [col, row] = [k % cols, Math.floor(k / cols)];
      const flip = g.group.odd_top && rows === 2 && row === 0;
      // Normal: number, jack, bars, caption. Flipped: caption, bars, jack, number.
      let at = 0;
      const next = (h: number, on: boolean) => {
        const y = at;
        if (on) at += h + GAP;
        return y;
      };
      const pieces = flip
        ? {
            captionY: next(CAPTION_H, flags.captions),
            barsY: next(BARS_H, flags.bars),
            jackY: next(slotH, true),
            numberY: next(NUMBER_H, flags.numbers),
          }
        : {
            numberY: next(NUMBER_H, flags.numbers),
            jackY: next(slotH, true),
            barsY: next(BARS_H, flags.bars),
            captionY: next(CAPTION_H, flags.captions),
          };
      jacks.push({
        port,
        group: g.group,
        number: portNumber(g.group, port),
        shape: jackShape(g.group.kind),
        flip,
        x: x + col * cellW,
        y: offset + row * (cellH + ROW_GAP),
        w: cellW,
        h: cellH,
        jackX: (cellW - jw) / 2,
        jackY: pieces.jackY + (slotH - jh) / 2,
        jackW: jw,
        jackH: jh,
        numberY: pieces.numberY,
        barsY: pieces.barsY,
        captionY: pieces.captionY,
      });
    });
    x += cols * cellW + GROUP_GAP;
  }
  const natural = x - GROUP_GAP;
  const k = natural > room && natural > 0 ? Math.max(MIN_SQUEEZE, room / natural) : 1;
  return {
    flags,
    height,
    width: natural * k,
    jacks:
      k === 1
        ? jacks
        : jacks.map((j) => {
            const cell = j.w * k;
            const w = Math.max(10, Math.min(j.jackW, j.jackW * k));
            return { ...j, x: j.x * k, w: cell, jackW: w, jackX: (cell - w) / 2 };
          }),
  };
}

/** The room a panel has in a box, by where it is placed. */
export function panelRoom(box: Pick<MapBox, "w" | "shape" | "panel">): number {
  const inner = box.w - 2 * boxPadX(box) - 2 * INSET;
  return box.panel?.place === "side" ? Math.floor(inner * 0.62) : inner;
}

/** A panel's outer size: the jacks plus the inset strip around them. */
export const panelOuter = (g: PanelGeometry): { w: number; h: number } => ({
  w: Math.ceil(g.width) + 2 * INSET,
  h: Math.ceil(g.height) + 2 * INSET,
});

/** A box carrying the panel origin the browser measured (set by `useMeasuredDoc`). */
export type MeasuredBox = MapBox & { panelOrigin?: { x: number; y: number } };

/** Where the panel's jacks start in the box: measured, else worked out from the layout. */
function originOf(box: MeasuredBox, g: PanelGeometry): { x: number; y: number } {
  if (box.panelOrigin) return box.panelOrigin;
  const pad = boxPadX(box);
  const { w, h } = panelOuter(g);
  const place = box.panel?.place ?? "bottom";
  if (place === "side") return { x: box.w - pad - w + INSET, y: CONTENT_TOP + INSET };
  if (place === "top") return { x: pad + INSET, y: 12 + INSET };
  return { x: pad + INSET, y: box.h - h - 12 + INSET };
}

/** The panel of a box with every jack's body in drawing coordinates, or null without ports. */
export function boxPanel(
  box: MapBox,
  device: readonly DevicePort[] | undefined,
): { geometry: PanelGeometry; rects: Map<string, Rect> } | null {
  if (!box.panel) return null;
  const geometry = panelGeometry(box.panel, device, panelRoom(box));
  if (!geometry) return null;
  const origin = originOf(box, geometry);
  const rects = new Map<string, Rect>();
  for (const j of geometry.jacks) {
    rects.set(j.port.id, {
      x: box.x + origin.x + j.x + j.jackX,
      y: box.y + origin.y + j.y + j.jackY,
      w: j.jackW,
      h: j.jackH,
    });
  }
  return { geometry, rects };
}
