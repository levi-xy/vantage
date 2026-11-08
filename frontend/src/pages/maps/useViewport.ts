// Pan and zoom of a map drawing, shared by the view and the editor: the view (offset and
// zoom of the surface inside its box), Fit, wheel and button zoom. The caller handles the
// pointer for panning, because the editor also uses the pointer for moving elements.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import type { MapDocument } from "../../api";
import { bounds } from "./geometry";

const MIN_ZOOM = 0.2;
const MAX_ZOOM = 3;
const FIT_MARGIN = 32;
const clampZoom = (z: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));

export interface View {
  x: number;
  y: number;
  z: number;
}

/** `fitKey`: the drawing is fitted again whenever this value changes (and once on mount). */
export function useViewport(
  box: RefObject<HTMLDivElement | null>,
  doc: MapDocument,
  fitKey: unknown,
) {
  const [view, setView] = useState<View>({ x: 0, y: 0, z: 1 });

  const fit = () => {
    const el = box.current;
    const b = bounds(doc);
    // jsdom (and a hidden pane) reports no size: keep the natural view then.
    if (!el || el.clientWidth === 0 || b.w === 0) return setView({ x: 0, y: 0, z: 1 });
    const z = clampZoom(
      Math.min(
        (el.clientWidth - 2 * FIT_MARGIN) / b.w,
        (el.clientHeight - 2 * FIT_MARGIN) / b.h,
        1,
      ),
    );
    setView({
      z,
      x: (el.clientWidth - b.w * z) / 2 - b.x * z,
      y: (el.clientHeight - b.h * z) / 2 - b.y * z,
    });
  };

  const fitted = useRef<unknown>(undefined);
  useEffect(() => {
    if (fitted.current !== fitKey) {
      fitted.current = fitKey;
      fit();
    }
  });

  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((v) => {
      const z = clampZoom(v.z * factor);
      const k = z / v.z;
      return { z, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k };
    });
  }, []);

  // A wheel listener that can stop the page scrolling (React's onWheel is passive).
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [box, zoomAt]);

  const centerZoom = (factor: number) => {
    const el = box.current;
    zoomAt(factor, (el?.clientWidth ?? 0) / 2, (el?.clientHeight ?? 0) / 2);
  };

  return { view, setView, fit, centerZoom };
}
