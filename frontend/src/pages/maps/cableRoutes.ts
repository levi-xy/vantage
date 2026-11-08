// Where every cable of a map runs, shared by the drawing and the editor (which needs the same
// points for its handles). A cable that names a port of a box with a strip meets that jack;
// otherwise it meets the box edge.

import type { MapCable, MapCableEnd, MapDocument } from "../../api";
import { endElements } from "./cableLive";
import { cablePath, endRects } from "./geometry";
import type { Point, Rect } from "./geometry";
import type { MapLive } from "./mapLive";
import { boxStrip } from "./strip";

export interface Routes {
  /** The rectangle of an end: its jack when it names one on a strip, else its box or frame. */
  endRect: (end: MapCableEnd) => Rect | undefined;
  /** The points of one cable, or undefined when an end is missing. */
  points: (cable: MapCable) => Point[] | undefined;
}

export function cableRoutes(doc: MapDocument, live: MapLive): Routes {
  const rects = endRects(doc);
  const strips = new Map<string, ReturnType<typeof boxStrip>>();
  for (const el of endElements(doc).values()) {
    if (el.kind === "box") strips.set(el.id, boxStrip(el, live.ports(el.instance_id)));
  }
  const endRect = (end: MapCableEnd) =>
    (end.port !== null ? strips.get(end.element)?.rects.get(end.port) : undefined) ??
    rects.get(end.element);
  return {
    endRect,
    points: (cable) => {
      const [boxA, boxB] = [rects.get(cable.from.element), rects.get(cable.to.element)];
      const [a, z] = [endRect(cable.from), endRect(cable.to)];
      if (!boxA || !boxB || !a || !z) return undefined;
      return cablePath(a, z, cable.bends, boxA, boxB);
    },
  };
}
