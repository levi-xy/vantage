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

const MeasureContext = createContext<Report>(() => undefined);
export const MeasureProvider = MeasureContext.Provider;

export type Heights = ReadonlyMap<string, number>;

/** The document with every measured box at its real height. */
export function withHeights(doc: MapDocument, heights: Heights): MapDocument {
  if (heights.size === 0) return doc;
  return {
    ...doc,
    elements: doc.elements.map((e) => {
      if (e.kind !== "box") return e;
      const h = heights.get(e.id);
      return h !== undefined && h !== e.h ? { ...e, h } : e;
    }),
  };
}

/** The measured heights of the boxes in view, the document with them applied, and the callback boxes report with. */
export function useMeasuredDoc(doc: MapDocument): {
  shown: MapDocument;
  heights: Heights;
  report: Report;
} {
  const [heights, setHeights] = useState<Heights>(new Map());
  const report = useCallback<Report>((id, height) => {
    setHeights((prev) => {
      if ((prev.get(id) ?? null) === height) return prev;
      const next = new Map(prev);
      if (height === null) next.delete(id);
      else next.set(id, height);
      return next;
    });
  }, []);
  const shown = useMemo(() => withHeights(doc, heights), [doc, heights]);
  return { shown, heights, report };
}

/** Reports the element's rendered height (border box, unaffected by zoom) while it is mounted. */
export function useReportHeight(id: string): RefObject<HTMLDivElement> {
  const report = useContext(MeasureContext);
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
