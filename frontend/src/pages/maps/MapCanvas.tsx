import { useCallback, useRef, useState } from "react";
import type { ReactNode, PointerEvent as ReactPointerEvent } from "react";
import type { MapDocument } from "../../api";
import { ZoomControls } from "./ZoomControls";
import { boxRate, portCables } from "./cableLive";
import { CableCard, DetailsDrawer } from "./MapDetails";
import { MeasureProvider, useMeasuredDoc } from "./measure";
import {
  BoxView,
  CablesLayer,
  FrameView,
  LegendView,
  NetworksView,
  TableView,
  TextView,
} from "./MapElements";
import type { CableHover } from "./MapElements";
import type { MapLive } from "./mapLive";
import { useViewport } from "./useViewport";

/**
 * The read-only drawing: elements at their saved x/y/w/h inside a pan (drag) and zoom
 * (wheel, +/-, Fit) viewport. Cables sit in an SVG layer under the boxes.
 */
export function MapCanvas({
  doc: saved,
  live,
  actions,
}: {
  doc: MapDocument;
  live: MapLive;
  /** Shown next to the zoom controls (the Edit button). */
  actions?: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  // Boxes are as tall as their content: cables and the fit use the measured heights.
  const { shown: doc, heights, report } = useMeasuredDoc(saved);
  const cables = portCables(doc, live);
  // Fit once at first, and again when the first measurement arrives or the map changes.
  const { view, setView, fit, centerZoom } = useViewport(box, doc, heights.size > 0 ? saved : null);
  const drag = useRef<{ px: number; py: number; vx: number; vy: number } | null>(null);
  // A drag that moved is a pan, not a click on the box under the pointer.
  const moved = useRef(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [hover, setHover] = useState<{ id: string; x: number; y: number; w: number } | null>(null);
  const select = useCallback((id: string) => {
    if (!moved.current) setSelected(id);
  }, []);
  const closeDrawer = useCallback(() => setSelected(null), []);
  const onCableHover = (h: CableHover | null) => {
    const r = box.current?.getBoundingClientRect();
    setHover(h && r ? { id: h.id, x: h.x - r.left, y: h.y - r.top, w: r.width } : null);
  };
  // Boxes are drawn at their stored width and height (the measured doc is for cables and fit).
  const stored = new Map(
    saved.elements.flatMap((e) => (e.kind === "box" ? [[e.id, e] as const] : [])),
  );
  const vlans = new Map(doc.vlans.map((v) => [v.id, v] as const));

  const onDown = (e: ReactPointerEvent) => {
    if (e.button !== 0) return;
    drag.current = { px: e.clientX, py: e.clientY, vx: view.x, vy: view.y };
    moved.current = false;
  };
  const onMove = (e: ReactPointerEvent) => {
    const d = drag.current;
    if (!d) return;
    // Capture the pointer only once it is really a pan, so a plain click reaches the box.
    if (!moved.current && Math.hypot(e.clientX - d.px, e.clientY - d.py) > 4) {
      moved.current = true;
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }
    if (moved.current) {
      setView((v) => ({ ...v, x: d.vx + e.clientX - d.px, y: d.vy + e.clientY - d.py }));
    }
  };
  const onUp = () => {
    drag.current = null;
  };

  return (
    <section aria-label="Network map" className="flex min-h-0 flex-1 flex-col">
      <div
        ref={box}
        className="relative min-h-[320px] flex-1 cursor-grab touch-none overflow-hidden bg-canvas select-none active:cursor-grabbing"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
      >
        <div
          data-testid="map-surface"
          className="absolute top-0 left-0 origin-top-left"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }}
        >
          <MeasureProvider value={report}>
            {/* Frames first, then the cables, then everything that sits on top. */}
            {doc.elements.map((el) =>
              el.kind === "frame" ? (
                <FrameView key={el.id} frame={el} live={live} onSelect={select} />
              ) : null,
            )}
            <CablesLayer doc={doc} live={live} onHover={onCableHover} />
            {doc.elements.map((el) => {
              switch (el.kind) {
                case "box":
                  return (
                    <BoxView
                      key={el.id}
                      box={stored.get(el.id) ?? el}
                      live={live}
                      vlans={vlans}
                      onSelect={select}
                      rate={el.shape === "cloud" ? boxRate(doc, el.id, live) : null}
                      cabled={cables.get(el.id)}
                    />
                  );
                case "text":
                  return <TextView key={el.id} el={el} />;
                case "table":
                  return <TableView key={el.id} el={el} />;
                case "legend":
                  return <LegendView key={el.id} el={el} doc={doc} />;
                case "networks":
                  return <NetworksView key={el.id} el={el} doc={doc} />;
                default:
                  return null;
              }
            })}
            <CablesLayer overlay doc={doc} live={live} />
          </MeasureProvider>
        </div>
        <ZoomControls zoom={view.z} onZoom={centerZoom} onFit={fit} actions={actions} />
        {hover && !selected && (
          <div
            className="pointer-events-none absolute z-10"
            style={{
              left: Math.min(hover.x + 14, Math.max(0, hover.w - 272)),
              top: hover.y + 14,
            }}
          >
            <CableCard cableId={hover.id} doc={doc} live={live} />
          </div>
        )}
        {selected && (
          <DetailsDrawer
            key={selected}
            elementId={selected}
            doc={doc}
            live={live}
            onClose={closeDrawer}
          />
        )}
      </div>
    </section>
  );
}
