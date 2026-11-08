// Boxes take their height from their content, so the drawing has to learn it: each box
// reports its rendered height, and cables, selection and fitting use the measured rectangle
// (the stored `h` is only the fallback before the first measurement, and in tests).

import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { RefObject } from "react";
import type { MapDocument } from "../../api";

type Report = (id: string, height: number | null) => void;
type Origin = { x: number; y: number };
type ReportOrigin = (id: string, origin: Origin | null) => void;

/** What the boxes report: their height, and where their front panel starts. */
export interface Reporter {
  height: Report;
  origin: ReportOrigin;
}

const MeasureContext = createContext<Reporter>({
  height: () => undefined,
  origin: () => undefined,
});
export const MeasureProvider = MeasureContext.Provider;

export type Heights = ReadonlyMap<string, number>;
export type Origins = ReadonlyMap<string, Origin>;

/** The document with every measured box at its real height and its panel origin. */
export function withHeights(
  doc: MapDocument,
  heights: Heights,
  origins: Origins = new Map(),
): MapDocument {
  if (heights.size === 0 && origins.size === 0) return doc;
  return {
    ...doc,
    elements: doc.elements.map((e) => {
      if (e.kind !== "box" && e.kind !== "frame" && e.kind !== "networks" && e.kind !== "legend") {
        return e;
      }
      const h = heights.get(e.id);
      const panelOrigin = e.kind === "box" ? origins.get(e.id) : undefined;
      if ((h === undefined || h === e.h) && !panelOrigin) return e;
      return { ...e, h: h ?? e.h, ...(panelOrigin ? { panelOrigin } : {}) };
    }),
  };
}

/** The measured heights of the boxes in view, the document with them applied, and the callback boxes report with. */
export function useMeasuredDoc(doc: MapDocument): {
  shown: MapDocument;
  heights: Heights;
  report: Reporter;
} {
  const [heights, setHeights] = useState<Heights>(new Map());
  const [origins, setOrigins] = useState<Origins>(new Map());
  const height = useCallback<Report>((id, value) => {
    setHeights((prev) => {
      if ((prev.get(id) ?? null) === value) return prev;
      const next = new Map(prev);
      if (value === null) next.delete(id);
      else next.set(id, value);
      return next;
    });
  }, []);
  const origin = useCallback<ReportOrigin>((id, value) => {
    setOrigins((prev) => {
      const old = prev.get(id) ?? null;
      if (old?.x === value?.x && old?.y === value?.y) return prev;
      const next = new Map(prev);
      if (value === null) next.delete(id);
      else next.set(id, value);
      return next;
    });
  }, []);
  const report = useMemo(() => ({ height, origin }), [height, origin]);
  const shown = useMemo(() => withHeights(doc, heights, origins), [doc, heights, origins]);
  return { shown, heights, report };
}

/** Reports the element's rendered height (border box, unaffected by zoom) while it is mounted. */
export function useReportHeight(id: string): RefObject<HTMLDivElement> {
  const report = useContext(MeasureContext).height;
  const ref = useRef<HTMLDivElement>(null) as RefObject<HTMLDivElement>;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // jsdom and hidden panes report 0: keep the stored height then.
    const send = () => {
      if (el.offsetHeight > 0) report(id, el.offsetHeight);
    };
    send();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(send);
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      report(id, null);
    };
  }, [id, report]);
  return ref;
}

/**
 * Reports where the element (a box's panel canvas) starts inside its box, in drawing pixels
 * (undone zoom), so cables meet the jacks where they really are. Follows the box's size.
 */
export function useReportPanelOrigin(id: string): RefObject<HTMLDivElement> {
  const report = useContext(MeasureContext).origin;
  const ref = useRef<HTMLDivElement>(null) as RefObject<HTMLDivElement>;
  useLayoutEffect(() => {
    const el = ref.current;
    const root = el?.closest<HTMLElement>("[data-element]");
    if (!el || !root) return;
    const send = () => {
      if (root.offsetWidth === 0) return; // jsdom and hidden panes: the layout's own numbers
      const r = root.getBoundingClientRect();
      const p = el.getBoundingClientRect();
      const k = r.width / root.offsetWidth || 1;
      report(id, { x: Math.round((p.left - r.left) / k), y: Math.round((p.top - r.top) / k) });
    };
    send();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(send);
    observer?.observe(root);
    return () => {
      observer?.disconnect();
      report(id, null);
    };
  }, [id, report]);
  return ref;
}
