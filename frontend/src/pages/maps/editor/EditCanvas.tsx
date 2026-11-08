// The drawing in edit mode: the same renderers as the view, plus selection outlines, resize
// handles, alignment guides and a dot grid. Pointer handling is done here (no library):
// press on an element selects and drags it, press on a handle resizes, press on the
// background pans (or places the tool's new element).

import { useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";
import type { MapDocument } from "../../../api";
import { buttonSecondary } from "../../../components/styles";
import { BoxView, CablesLayer, FrameView, LegendView, TableView, TextView } from "../MapElements";
import type { MapLive } from "../mapLive";
import type { useViewport } from "../useViewport";
import {
  alignTo,
  applyRects,
  clamp,
  GRID,
  HANDLES,
  MAX_COORD,
  rectsOf,
  resizeRect,
  snapTo,
  union,
} from "./model";
import type { Guides, Handle, Rect } from "./model";

const DRAG_START_PX = 3;
const ALIGN_PX = 6;
const ELEMENT_ID = "application/x-vantage-instance";

type Drag =
  | { mode: "pan"; px: number; py: number; vx: number; vy: number; moved: boolean }
  | {
      mode: "move";
      sx: number;
      sy: number;
      base: MapDocument;
      ids: string[];
      orig: Map<string, Rect>;
      moved: boolean;
    }
  | {
      mode: "resize";
      handle: Handle;
      sx: number;
      sy: number;
      base: MapDocument;
      id: string;
      rect: Rect;
    };

export function EditCanvas({
  doc,
  box,
  viewport,
  live,
  selected,
  placing,
  snap,
  onSelect,
  onPlace,
  onDropInstance,
  preview,
  commitFrom,
}: {
  doc: MapDocument;
  box: RefObject<HTMLDivElement>;
  viewport: ReturnType<typeof useViewport>;
  live: MapLive;
  selected: string[];
  /** A tool is armed: a click on the drawing places its element. */
  placing: boolean;
  snap: boolean;
  onSelect: (ids: string[]) => void;
  onPlace: (x: number, y: number) => void;
  onDropInstance: (instanceId: string, x: number, y: number) => void;
  preview: (doc: MapDocument) => void;
  commitFrom: (base: MapDocument) => void;
}) {
  const { view, setView, fit, centerZoom } = viewport;
  const drag = useRef<Drag | null>(null);
  const [guides, setGuides] = useState<Guides>({ v: [], h: [] });
  const vlans = new Map(doc.vlans.map((v) => [v.id, v] as const));
  const single = selected.length === 1 ? selected[0] : undefined;

  /** The pointer in drawing coordinates. */
  const at = (e: { clientX: number; clientY: number }): [number, number] => {
    const r = box.current?.getBoundingClientRect();
    return [
      (e.clientX - (r?.left ?? 0) - view.x) / view.z,
      (e.clientY - (r?.top ?? 0) - view.y) / view.z,
    ];
  };

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const target = e.target as Element;
    const [px, py] = at(e);
    const handle = target.closest("[data-handle]")?.getAttribute("data-handle") as Handle | null;
    if (handle && single) {
      const rect = rectsOf(doc, [single]).get(single);
      if (rect) {
        drag.current = { mode: "resize", handle, sx: px, sy: py, base: doc, id: single, rect };
        e.currentTarget.setPointerCapture?.(e.pointerId);
        return;
      }
    }
    if (placing) {
      onPlace(px, py);
      return;
    }
    const id = target.closest("[data-element]")?.getAttribute("data-element");
    if (id) {
      let ids = selected;
      if (e.shiftKey) {
        ids = selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id];
        onSelect(ids);
        if (!ids.includes(id)) return;
      } else if (!selected.includes(id)) {
        ids = [id];
        onSelect(ids);
      }
      drag.current = {
        mode: "move",
        sx: px,
        sy: py,
        base: doc,
        ids,
        orig: rectsOf(doc, ids),
        moved: false,
      };
      return;
    }
    drag.current = {
      mode: "pan",
      px: e.clientX,
      py: e.clientY,
      vx: view.x,
      vy: view.y,
      moved: false,
    };
  };

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    if (d.mode === "pan") {
      if (!d.moved && Math.hypot(e.clientX - d.px, e.clientY - d.py) > DRAG_START_PX) {
        d.moved = true;
        e.currentTarget.setPointerCapture?.(e.pointerId);
      }
      if (d.moved) {
        setView((v) => ({ ...v, x: d.vx + e.clientX - d.px, y: d.vy + e.clientY - d.py }));
      }
      return;
    }
    const [px, py] = at(e);
    if (d.mode === "resize") {
      const next = resizeRect(d.rect, d.handle, px - d.sx, py - d.sy, snap);
      preview(applyRects(d.base, new Map([[d.id, next]])));
      return;
    }
    if (!d.moved) {
      if (Math.hypot(px - d.sx, py - d.sy) * view.z <= DRAG_START_PX) return;
      d.moved = true;
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }
    const group = union(d.orig.values());
    let nx = group.x + px - d.sx;
    let ny = group.y + py - d.sy;
    if (snap) {
      nx = snapTo(nx);
      ny = snapTo(ny);
    }
    const others = d.base.elements.flatMap((el) =>
      el.kind === "cable" || d.ids.includes(el.id) ? [] : [{ x: el.x, y: el.y, w: el.w, h: el.h }],
    );
    const pulled = alignTo({ ...group, x: nx, y: ny }, others, ALIGN_PX / view.z);
    nx = clamp(nx + pulled.dx, 0, MAX_COORD);
    ny = clamp(ny + pulled.dy, 0, MAX_COORD);
    setGuides(pulled.guides);
    const moved = new Map<string, Rect>();
    for (const [id, r] of d.orig) {
      moved.set(id, { ...r, x: Math.round(r.x + nx - group.x), y: Math.round(r.y + ny - group.y) });
    }
    preview(applyRects(d.base, moved));
  };

  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    setGuides({ v: [], h: [] });
    if (!d) return;
    if (d.mode === "pan") {
      if (!d.moved) onSelect([]);
      return;
    }
    if (d.mode === "resize" || d.moved) commitFrom(d.base);
  };

  const selectedRects = doc.elements.flatMap((el) =>
    el.kind !== "cable" && selected.includes(el.id)
      ? [{ id: el.id, x: el.x, y: el.y, w: el.w, h: el.h }]
      : [],
  );
  const cable = selected.length === 1 ? selected[0] : null;
  const handleSize = 10 / view.z;
  const handleAt = (r: Rect, h: Handle) => ({
    left: r.x + (h.includes("w") ? 0 : h.includes("e") ? r.w : r.w / 2) - handleSize / 2,
    top: r.y + (h.includes("n") ? 0 : h.includes("s") ? r.h : r.h / 2) - handleSize / 2,
  });
  const cursor: Record<Handle, string> = {
    n: "ns-resize",
    s: "ns-resize",
    e: "ew-resize",
    w: "ew-resize",
    ne: "nesw-resize",
    sw: "nesw-resize",
    nw: "nwse-resize",
    se: "nwse-resize",
  };

  return (
    <section aria-label="Map editor">
      <div className="mb-2 flex items-center justify-end gap-2">
        <button
          type="button"
          aria-label="Zoom out"
          className={buttonSecondary}
          onClick={() => centerZoom(1 / 1.25)}
        >
          −
        </button>
        <span className="w-12 text-center text-sm text-muted tabular-nums">
          {Math.round(view.z * 100)}%
        </span>
        <button
          type="button"
          aria-label="Zoom in"
          className={buttonSecondary}
          onClick={() => centerZoom(1.25)}
        >
          +
        </button>
        <button type="button" className={buttonSecondary} onClick={fit}>
          Fit
        </button>
      </div>
      <div
        ref={box}
        data-testid="edit-viewport"
        className={`relative h-[calc(100vh-260px)] min-h-[420px] touch-none overflow-hidden rounded-lg border border-line bg-canvas select-none ${placing ? "cursor-crosshair" : ""}`}
        style={{
          backgroundImage: "radial-gradient(circle, var(--vt-line) 1px, transparent 1.5px)",
          backgroundSize: `${GRID * view.z}px ${GRID * view.z}px`,
          backgroundPosition: `${view.x}px ${view.y}px`,
        }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          const id = e.dataTransfer.getData(ELEMENT_ID);
          if (!id) return;
          e.preventDefault();
          const [x, y] = at(e);
          onDropInstance(id, x, y);
        }}
      >
        <div
          data-testid="map-surface"
          className="absolute top-0 left-0 origin-top-left"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }}
        >
          {doc.elements.map((el) =>
            el.kind === "frame" ? <FrameView key={el.id} frame={el} live={live} /> : null,
          )}
          <CablesLayer doc={doc} live={live} pickedId={cable} onPick={(id) => onSelect([id])} />
          {doc.elements.map((el) => {
            switch (el.kind) {
              case "box":
                return <BoxView key={el.id} box={el} live={live} vlans={vlans} />;
              case "text":
                return <TextView key={el.id} el={el} />;
              case "table":
                return <TableView key={el.id} el={el} />;
              case "legend":
                return <LegendView key={el.id} el={el} doc={doc} />;
              default:
                return null;
            }
          })}
          {selectedRects.map((r) => (
            <div
              key={r.id}
              data-selected={r.id}
              className="pointer-events-none absolute rounded-[10px] border-2 border-in"
              style={{ left: r.x - 1, top: r.y - 1, width: r.w + 2, height: r.h + 2 }}
            />
          ))}
          {single &&
            selectedRects.length === 1 &&
            HANDLES.map((h) => (
              <div
                key={h}
                data-handle={h}
                className="absolute rounded-sm border border-in bg-panel"
                style={{
                  ...handleAt(selectedRects[0] as Rect, h),
                  width: handleSize,
                  height: handleSize,
                  cursor: cursor[h],
                }}
              />
            ))}
          {guides.v.map((x) => (
            <div
              key={`v${x}`}
              data-guide="v"
              className="pointer-events-none absolute top-0 bg-out"
              style={{ left: x, width: 1 / view.z, height: MAX_COORD }}
            />
          ))}
          {guides.h.map((y) => (
            <div
              key={`h${y}`}
              data-guide="h"
              className="pointer-events-none absolute left-0 bg-out"
              style={{ top: y, height: 1 / view.z, width: MAX_COORD }}
            />
          ))}
        </div>
        {doc.elements.length === 0 && (
          <p className="pointer-events-none absolute inset-x-0 top-1/3 text-center text-sm text-muted">
            Pick a tool on the left and click here, or drag a device in from the list.
          </p>
        )}
      </div>
    </section>
  );
}

export { ELEMENT_ID as DRAG_DEVICE_TYPE };
