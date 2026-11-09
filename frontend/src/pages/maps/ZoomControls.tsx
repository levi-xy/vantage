import type { ReactNode } from "react";
import { buttonSecondary } from "../../components/styles";

/**
 * Zoom out / in / Fit, floating in the bottom-left corner of a map viewport so the drawing
 * keeps the whole area. A press here never starts a pan or a click on the map below.
 */
export function ZoomControls({
  zoom,
  onZoom,
  onFit,
  actions,
}: {
  zoom: number;
  onZoom: (factor: number) => void;
  onFit: () => void;
  actions?: ReactNode;
}) {
  return (
    <div
      className="absolute bottom-3 left-3 z-10 flex cursor-default items-center gap-2 rounded-lg border border-line bg-panel p-1.5 shadow-md"
      onPointerDown={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        aria-label="Zoom out"
        className={buttonSecondary}
        onClick={() => onZoom(1 / 1.25)}
      >
        −
      </button>
      <span className="w-12 text-center text-sm text-muted tabular-nums">
        {Math.round(zoom * 100)}%
      </span>
      <button
        type="button"
        aria-label="Zoom in"
        className={buttonSecondary}
        onClick={() => onZoom(1.25)}
      >
        +
      </button>
      <button type="button" className={buttonSecondary} onClick={onFit}>
        Fit
      </button>
      {actions}
    </div>
  );
}
