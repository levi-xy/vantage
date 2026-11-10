// A box taller than its content scales the content up to fill (uniform, never below 1, so
// nothing is ever clipped). The box's natural height is measured with the content at scale 1;
// the content is then laid out `w / scale` wide and drawn `scale` times larger, so it fills
// the stored width and height. A stored height equal to the editor's default is "not chosen".

import { useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import type { MapBox } from "../../api";
import { DEFAULT_BOX_H } from "./boxFacts";

export interface Fit {
  /** Content scale, 1 = natural size. */
  scale: number;
  /** Space above the content that centres it vertically, in box pixels. */
  dy: number;
  /** The box's height when scaled up (undefined = as tall as its content). */
  height: number | undefined;
}

const NATURAL: Fit = { scale: 1, dy: 0, height: undefined };

/** The scale that makes `natural` tall content fill `room`, 1 when there is nothing to fill. */
export function fitScale(room: number, natural: number): number {
  if (natural <= 0 || room <= natural * 1.02) return 1;
  return Math.round((room / natural) * 1000) / 1000;
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

type State = { deps: readonly unknown[]; fit: Fit; frame: number };

/**
 * The fit of a box: measured from `inner` (the wrapper of its content) inside `outer`.
 * `inner` is laid out at scale 1 on the first render after anything changed.
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
  const fit = enabled && fresh ? state.fit : NATURAL;
  const seen = useRef(0);

  // Measure-then-set before paint, on every render by design (it settles in two passes).
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */
  useLayoutEffect(() => {
    const el = inner.current;
    const root = outer.current;
    if (!enabled || !el || !root) return;
    if (!fresh) {
      // Rendered at scale 1: this is the natural size.
      const natural = el.offsetHeight;
      if (natural === 0) return; // jsdom and hidden panes
      const frame = root.offsetHeight - natural;
      seen.current = natural;
      const room = box.h === DEFAULT_BOX_H ? 0 : box.h - frame;
      setState({ deps, frame, fit: { scale: fitScale(room, natural), dy: 0, height: undefined } });
    } else if (state.fit.scale > 1) {
      // Scaled: centre it, and grow the box if the narrower layout came out taller.
      seen.current = el.offsetHeight;
      const drawn = el.offsetHeight * state.fit.scale;
      const height = Math.max(box.h, Math.ceil(drawn + state.frame));
      const dy = Math.max(0, Math.round((height - state.frame - drawn) / 2));
      if (state.fit.height !== height || state.fit.dy !== dy) {
        setState({ ...state, fit: { ...state.fit, dy, height } });
      }
    }
  });
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

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
