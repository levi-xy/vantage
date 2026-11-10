// The width of the editor's inspector: dragged or set with the arrow keys, remembered per
// browser (a preference only; storage may be blocked).

import { useState } from "react";

export const INSPECTOR_MIN = 280;
export const INSPECTOR_MAX = 640;
export const INSPECTOR_DEFAULT = 288;
const KEY = "vantage.mapInspectorWidth";

const clampWidth = (w: number) => Math.min(INSPECTOR_MAX, Math.max(INSPECTOR_MIN, Math.round(w)));

function read(): number {
  try {
    const n = Number(localStorage.getItem(KEY));
    return Number.isFinite(n) && n > 0 ? clampWidth(n) : INSPECTOR_DEFAULT;
  } catch {
    return INSPECTOR_DEFAULT;
  }
}

export function useInspectorWidth(): [number, (w: number) => void] {
  const [width, setWidth] = useState(read);
  const set = (w: number) => {
    const next = clampWidth(w);
    if (next === width) return;
    setWidth(next);
    try {
      localStorage.setItem(KEY, String(next));
    } catch {
      // Not remembered.
    }
  };
  return [width, set];
}
