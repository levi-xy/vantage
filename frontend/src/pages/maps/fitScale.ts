// A box bigger than its content scales the content up to fill (uniform, never below 1, so
// nothing is ever clipped or cropped). The content's natural size is measured at scale 1 in
// "shrink to fit" layout (as wide as the content needs, at most the box); the content is then
// drawn at scale = min(box width / natural width, box height / natural height), centred.
// Viewing and editing use the same stored width and height, so they scale identically.
// A stored height equal to a default the app once assigned means "not chosen".

import { useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, RefObject } from "react";
import type { MapBox } from "../../api";
import { isDefaultBoxH } from "./boxFacts";

export interface Fit {
  /** Content scale, 1 = natural size. */
  scale: number;
  /** The inner wrapper's style for this state. */
  style: CSSProperties | undefined;
  /** The box's height when scaled up (undefined = as tall as its content). */
  height: number | undefined;
}

const NATURAL: Fit = { scale: 1, style: undefined, height: undefined };
/** Rendered once at scale 1 so the natural size can be read. */
const MEASURING: Fit = {
  scale: 1,
  style: { width: "fit-content", maxWidth: "100%" },
  height: undefined,
};

/** The scale that makes natural-sized content fill the room, 1 when there is nothing to fill. */
export function fitScale(room: { w: number; h: number }, natural: { w: number; h: number }) {
  if (natural.w <= 0 || natural.h <= 0) return 1;
  const s = Math.min(room.w / natural.w, room.h / natural.h);
  return s <= 1.02 ? 1 : Math.round(s * 1000) / 1000;
}

// What changes a box's natural size (live figures change it through the observer below).
const depsOf = (b: MapBox) => [
  b.w,
  b.h,
  b.shape,
  b.label,
  b.subtitle,
  b.role,
  b.model,
  b.address,
  b.version,
  b.icon,
  b.panel,
  b.sections,
  b.show,
  b.figures,
  b.nas,
];

type State = { deps: readonly unknown[]; fit: Fit };

/**
 * The fit of a box: measured from `inner` (the wrapper of its content) inside `outer`.
 * The first render after anything changed shows `inner` at scale 1 and measures it.
 */
export function useFitScale(
  outer: RefObject<HTMLDivElement>,
  inner: RefObject<HTMLDivElement>,
  box: MapBox,
  enabled: boolean,
): Fit {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<State | null>(null);
  const deps = [...depsOf(box), tick];
  const fresh =
    state !== null &&
    state.deps.length === deps.length &&
    state.deps.every((d, i) => Object.is(d, deps[i]));
  const fit = !enabled ? NATURAL : fresh ? state.fit : MEASURING;
  const seen = useRef(0);

  // Measure before paint; only when something changed (the state then matches `deps`).
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */
  useLayoutEffect(() => {
    const el = inner.current;
    const root = outer.current;
    if (!enabled || fresh || !el || !root) return;
    const natural = { w: el.offsetWidth, h: el.offsetHeight };
    seen.current = natural.h;
    if (natural.h === 0 || natural.w === 0) {
      setState({ deps, fit: NATURAL }); // jsdom and hidden panes
      return;
    }
    const frameH = root.offsetHeight - natural.h;
    const room = { w: root.clientWidth, h: box.h - frameH };
    const scale = isDefaultBoxH(box.h) ? 1 : fitScale(room, natural);
    if (scale === 1) {
      setState({ deps, fit: NATURAL });
      return;
    }
    const dx = Math.max(0, Math.round((room.w - natural.w * scale) / 2));
    const dy = Math.max(0, Math.round((room.h - natural.h * scale) / 2));
    setState({
      deps,
      fit: {
        scale,
        height: box.h,
        style: {
          width: natural.w,
          transform: `translate(${dx}px, ${dy}px) scale(${scale})`,
          transformOrigin: "0 0",
        },
      },
    });
  });
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  // What the settled layout measures, so only a later change counts (never our own relayout).
  useLayoutEffect(() => {
    if (enabled && fresh && inner.current) seen.current = inner.current.offsetHeight;
  });

  // Content that changes height by itself (live figures, wrapping text) starts a new measurement.
  useLayoutEffect(() => {
    const el = inner.current;
    if (!enabled || !el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (seen.current > 0 && Math.abs(el.offsetHeight - seen.current) > 1) setTick((t) => t + 1);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [enabled, inner]);

  return fit;
}
