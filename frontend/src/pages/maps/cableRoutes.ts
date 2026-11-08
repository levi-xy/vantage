// Where every cable of a map runs, shared by the drawing and the editor (which needs the same
// points for its handles). A cable that names a port of a box with a front panel meets that jack;
// otherwise it meets the box edge.

import type { MapCable, MapCableEnd, MapDocument } from "../../api";
import { endElements } from "./cableLive";
import { inflate, nearby, overlap, placeLabel } from "./cableAvoid";
import { cablePath, endRects, labelPoint } from "./geometry";
import type { Point, Rect } from "./geometry";
import type { MapLive } from "./mapLive";
import { boxPanel } from "./panelGeometry";

export interface Routes {
  /** The rectangle of an end: its jack when it names one on a front panel, else its box or frame. */
  endRect: (end: MapCableEnd) => Rect | undefined;
  /** The points of one cable, or undefined when an end is missing. */
  points: (cable: MapCable) => Point[] | undefined;
  /** Where the cable's label goes: a free stretch of its route, clear of elements and labels. */
  labelAt: (cable: MapCable) => Point | undefined;
}

/** Routes worked out before, by cable: a cable whose ends and surroundings didn't change keeps its route. */
export type RouteCache = Map<string, { key: string; points: Point[] | undefined }>;

export function cableRoutes(doc: MapDocument, live: MapLive, cache?: RouteCache): Routes {
  const rects = endRects(doc);
  const panels = new Map<string, ReturnType<typeof boxPanel>>();
  for (const el of endElements(doc).values()) {
    if (el.kind === "box") panels.set(el.id, boxPanel(el, live.ports(el.instance_id)));
  }
  const endRect = (end: MapCableEnd) =>
    (end.port !== null ? panels.get(end.element)?.rects.get(end.port) : undefined) ??
    rects.get(end.element);
  // Everything a cable must keep clear of: every element but the cables, name tabs included.
  const things = doc.elements.flatMap((e) =>
    e.kind === "cable"
      ? []
      : [
          {
            id: e.id,
            rect: inflate(
              { x: e.x, y: e.y, w: e.w, h: e.h },
              e.kind === "box" || e.kind === "frame",
            ),
          },
        ],
  );
  const computed = new Map<string, Point[] | undefined>();
  const points = (cable: MapCable) => {
    if (computed.has(cable.id)) return computed.get(cable.id);
    const [boxA, boxB] = [rects.get(cable.from.element), rects.get(cable.to.element)];
    const [a, z] = [endRect(cable.from), endRect(cable.to)];
    let out: Point[] | undefined;
    if (boxA && boxB && a && z) {
      const zone = nearby(a, z);
      const avoid =
        cable.bends.length > 0
          ? []
          : things
              .filter((t) => t.id !== cable.from.element && t.id !== cable.to.element)
              .map((t) => t.rect)
              .filter((r) => overlap(r, zone));
      const key = JSON.stringify([a, z, boxA, boxB, cable.bends, avoid]);
      const old = cache?.get(cable.id);
      if (old && old.key === key) out = old.points;
      else {
        out = cablePath(a, z, cable.bends, boxA, boxB, avoid);
        cache?.set(cable.id, { key, points: out });
      }
    }
    computed.set(cable.id, out);
    return out;
  };
  let labels: Map<string, Point> | undefined;
  const labelAt = (cable: MapCable) => {
    if (!labels) {
      labels = new Map();
      const taken: Rect[] = [];
      const all = things.map((t) => t.rect);
      for (const c of doc.elements) {
        if (c.kind !== "cable" || (!c.label && !c.show_rate)) continue;
        const pts = points(c);
        if (!pts) continue;
        const width = Math.ceil((c.label.length + (c.show_rate ? 28 : 0)) * 6.6) + 4;
        const placed = placeLabel(pts, width, all, taken);
        const at = placed?.at ?? labelPoint(pts);
        taken.push(placed?.rect ?? { x: at[0] + 6, y: at[1] - 19, w: width, h: 17 });
        labels.set(c.id, at);
      }
    }
    return labels.get(cable.id);
  };
  return { endRect, points, labelAt };
}
