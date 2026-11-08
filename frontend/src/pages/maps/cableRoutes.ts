// Where every cable of a map runs, shared by the drawing and the editor (which needs the same
// points for its handles). A cable that names a port of a box with a front panel meets that jack;
// otherwise it meets the box edge.

import type { MapCable, MapCableEnd, MapDocument } from "../../api";
import { endElements } from "./cableLive";
import { cablePath, endRects } from "./geometry";
import type { Point, Rect } from "./geometry";
import type { MapLive } from "./mapLive";
import { boxPanel } from "./panelGeometry";

export interface Routes {
  /** The rectangle of an end: its jack when it names one on a front panel, else its box or frame. */
  endRect: (end: MapCableEnd) => Rect | undefined;
  /** The points of one cable, or undefined when an end is missing. */
  points: (cable: MapCable) => Point[] | undefined;
}

export function cableRoutes(doc: MapDocument, live: MapLive): Routes {
  const rects = endRects(doc);
  const panels = new Map<string, ReturnType<typeof boxPanel>>();
  for (const el of endElements(doc).values()) {
    if (el.kind === "box") panels.set(el.id, boxPanel(el, live.ports(el.instance_id)));
  }
  const endRect = (end: MapCableEnd) =>
    (end.port !== null ? panels.get(end.element)?.rects.get(end.port) : undefined) ??
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
