// The drawing in edit mode: the same renderers as the view, plus selection outlines, resize
// handles, alignment guides and a dot grid. Pointer handling is done here (no library):
// press on an element selects and drags it, press on a handle resizes, press on the
// background pans (or places the tool's new element).

import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";
import type { MapCable, MapCableEnd, MapDocument } from "../../../api";
import { buttonSecondary } from "../../../components/styles";
import { boxRate, portCables } from "../cableLive";
import { cableRoutes } from "../cableRoutes";
import type { RouteCache } from "../cableRoutes";
import { MeasureProvider, useMeasuredDoc } from "../measure";
import { contentSized } from "../boxFacts";
import {
  BoxView,
  CablesLayer,
  FrameView,
  LegendView,
  NetworksView,
  TableView,
  TextView,
} from "../MapElements";
import type { MapLive } from "../mapLive";
import type { useViewport } from "../useViewport";
import { bendIndexAt, insertBend, moveBend, removeBend } from "./mapOps";
import {
  alignTo,
  applyRects,
  clamp,
  GRID,
  HANDLES,
  MAX_COORD,
  patchElement,
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
    }
  | { mode: "cable"; from: MapCableEnd }
  | { mode: "reattach"; cableId: string; end: "from" | "to"; other: MapCableEnd }
  | {
      mode: "bend";
      sx: number;
      sy: number;
      base: MapDocument;
      cableId: string;
      /** The bend being moved, or null: a new one is made at `insertAt` once the drag starts. */
      index: number | null;
      insertAt: number;
      moved: boolean;
    };

/** A cable being drawn or re-attached: from a fixed point to the pointer. */
interface Draft {
  from: readonly [number, number];
  x: number;
  y: number;
  /** The element at the fixed end: dropping on it is refused. */
  fixed: string;
  over: MapCableEnd | null;
}

/** The box or frame (and its port jack, if any) under a pointer event. */
function endAt(e: { clientX: number; clientY: number; target: EventTarget }): MapCableEnd | null {
  const found = (document as Partial<Document>).elementFromPoint?.call(
    document,
    e.clientX,
    e.clientY,
  );
  const node = found ?? (e.target instanceof Element ? e.target : null);
  const owner = node?.closest("[data-element]");
  const kind = owner?.getAttribute("data-kind");
  const element = owner?.getAttribute("data-element");
  if (!owner || !element || (kind !== "box" && kind !== "frame")) return null;
  const jack = node?.closest("[data-port]");
  return { element, port: jack && owner.contains(jack) ? jack.getAttribute("data-port") : null };
}

/** A box is as tall as its content, so only its width can be dragged. */
const handlesFor = (doc: MapDocument, id: string): readonly Handle[] => {
  const el = doc.elements.find((e) => e.id === id);
  return el && contentSized(el) ? HANDLES.filter((h) => h === "e" || h === "w") : HANDLES;
};

export function EditCanvas({
  doc,
  box,
  viewport,
  live,
  selected,
  placing,
  cabling,
  snap,
  onSelect,
  onPlace,
  onConnect,
  onDropInstance,
  preview,
  commit,
  commitFrom,
}: {
  doc: MapDocument;
  box: RefObject<HTMLDivElement>;
  viewport: ReturnType<typeof useViewport>;
  live: MapLive;
  selected: string[];
  /** A tool is armed: a click on the drawing places its element. */
  placing: boolean;
  /** The cable tool is armed: a drag from a box or port to another draws a cable. */
  cabling: boolean;
  snap: boolean;
  onSelect: (ids: string[]) => void;
  onPlace: (x: number, y: number) => void;
  onConnect: (from: MapCableEnd, to: MapCableEnd) => void;
  commit: (doc: MapDocument) => void;
  onDropInstance: (instanceId: string, x: number, y: number) => void;
  preview: (doc: MapDocument) => void;
  commitFrom: (base: MapDocument) => void;
}) {
  const { view, setView, fit, centerZoom } = viewport;
  // Boxes are as tall as their content: cables, outlines and guides use the measured heights.
  const { shown, report } = useMeasuredDoc(doc);
  const drag = useRef<Drag | null>(null);
  const [guides, setGuides] = useState<Guides>({ v: [], h: [] });
  const vlans = new Map(doc.vlans.map((v) => [v.id, v] as const));
  const single = selected.length === 1 ? selected[0] : undefined;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [routeCache] = useState<RouteCache>(() => new Map());
  const routes = useMemo(() => cableRoutes(shown, live, routeCache), [shown, live, routeCache]);
  const cables = portCables(shown, live);
  const selCable = doc.elements.find((e): e is MapCable => e.kind === "cable" && e.id === single);
  const selPoints = selCable ? routes.points(selCable) : undefined;

  const centreOf = (end: MapCableEnd): [number, number] => {
    const r = routes.endRect(end);
    return r ? [r.x + r.w / 2, r.y + r.h / 2] : [0, 0];
  };

  // Escape cancels a cable being drawn.
  useEffect(() => {
    if (!draft) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      drag.current = null;
      setDraft(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [draft]);

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
    if (selCable && selPoints) {
      const endHandle = target.closest("[data-end-handle]")?.getAttribute("data-end-handle");
      if (endHandle === "from" || endHandle === "to") {
        const other = endHandle === "from" ? selCable.to : selCable.from;
        drag.current = { mode: "reattach", cableId: selCable.id, end: endHandle, other };
        setDraft({ from: centreOf(other), x: px, y: py, fixed: other.element, over: null });
        return;
      }
      const bend = target.closest("[data-bend]")?.getAttribute("data-bend");
      const onSegment = target.closest("[data-hit]")?.getAttribute("data-hit") === selCable.id;
      if (bend !== null && bend !== undefined) {
        drag.current = {
          mode: "bend",
          sx: px,
          sy: py,
          base: doc,
          cableId: selCable.id,
          index: Number(bend),
          insertAt: 0,
          moved: false,
        };
        e.currentTarget.setPointerCapture?.(e.pointerId);
        return;
      }
      if (onSegment && !cabling) {
        const way = [selPoints[0], ...selCable.bends, selPoints[selPoints.length - 1]].flatMap(
          (pt) => (pt ? [pt] : []),
        );
        drag.current = {
          mode: "bend",
          sx: px,
          sy: py,
          base: doc,
          cableId: selCable.id,
          index: null,
          insertAt: bendIndexAt(way, px, py),
          moved: false,
        };
        e.currentTarget.setPointerCapture?.(e.pointerId);
        return;
      }
    }
    if (cabling) {
      const from = endAt(e);
      if (from) {
        drag.current = { mode: "cable", from };
        setDraft({ from: centreOf(from), x: px, y: py, fixed: from.element, over: null });
        return;
      }
    }
    if (placing) {
      onPlace(px, py);
      return;
    }
    const hit = target.closest("[data-hit]")?.getAttribute("data-hit");
    if (hit) {
      onSelect([hit]);
      return;
    }
    const id = target.closest("[data-element]")?.getAttribute("data-element");
    if (id && !cabling) {
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
    if (d.mode === "cable" || d.mode === "reattach") {
      const over = endAt(e);
      setDraft(
        (c) => c && { ...c, x: px, y: py, over: over && over.element !== c.fixed ? over : null },
      );
      return;
    }
    if (d.mode === "bend") {
      if (!d.moved) {
        if (Math.hypot(px - d.sx, py - d.sy) * view.z <= DRAG_START_PX) return;
        d.moved = true;
      }
      const cable = d.base.elements.find((el): el is MapCable => el.id === d.cableId);
      if (!cable) return;
      const bends =
        d.index === null
          ? insertBend(cable.bends, d.insertAt, px, py)
          : moveBend(cable.bends, d.index, px, py);
      preview(patchElement(d.base, d.cableId, { bends }));
      return;
    }
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
    const others = shown.elements.flatMap((el) =>
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

  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    setGuides({ v: [], h: [] });
    if (!d) return;
    if (d.mode === "cable" || d.mode === "reattach") {
      setDraft(null);
      const to = endAt(e);
      const fixed = d.mode === "cable" ? d.from.element : d.other.element;
      if (!to || to.element === fixed) return;
      if (d.mode === "cable") onConnect(d.from, to);
      else commit(patchElement(doc, d.cableId, { [d.end]: to }));
      return;
    }
    if (d.mode === "bend") {
      if (d.moved) commitFrom(d.base);
      return;
    }
    if (d.mode === "pan") {
      if (!d.moved) onSelect([]);
      return;
    }
    if (d.mode === "resize" || d.moved) commitFrom(d.base);
  };

  const selectedRects = shown.elements.flatMap((el) =>
    el.kind !== "cable" && selected.includes(el.id)
      ? [{ id: el.id, x: el.x, y: el.y, w: el.w, h: el.h }]
      : [],
  );
  const cable = selCable?.id ?? null;
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
        className={`relative h-[calc(100vh-260px)] min-h-[420px] touch-none overflow-hidden rounded-lg border border-line bg-canvas select-none ${placing || cabling ? "cursor-crosshair" : ""}`}
        style={{
          backgroundImage: "radial-gradient(circle, var(--vt-line) 1px, transparent 1.5px)",
          backgroundSize: `${GRID * view.z}px ${GRID * view.z}px`,
          backgroundPosition: `${view.x}px ${view.y}px`,
        }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onPointerLeave={() => {
          // A cable dragged out of the drawing is dropped nowhere.
          if (drag.current?.mode === "cable" || drag.current?.mode === "reattach") {
            drag.current = null;
            setDraft(null);
          }
        }}
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
          <MeasureProvider value={report}>
            {doc.elements.map((el) =>
              el.kind === "frame" ? <FrameView key={el.id} frame={el} live={live} /> : null,
            )}
            <CablesLayer doc={shown} live={live} pickedId={cable} onPick={(id) => onSelect([id])} />
            {doc.elements.map((el) => {
              switch (el.kind) {
                case "box":
                  return (
                    <BoxView
                      key={el.id}
                      box={el}
                      live={live}
                      vlans={vlans}
                      rate={el.shape === "cloud" ? boxRate(shown, el.id, live) : null}
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
              handlesFor(shown, single).map((h) => (
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
            {selCable && selPoints && (
              <>
                {(["from", "to"] as const).map((end) => {
                  const p = end === "from" ? selPoints[0] : selPoints[selPoints.length - 1];
                  const size = 12 / view.z;
                  return (
                    p && (
                      <div
                        key={end}
                        data-end-handle={end}
                        title="Drag to connect this end elsewhere"
                        className="absolute cursor-grab rounded-full border-2 border-in bg-panel"
                        style={{
                          left: p[0] - size / 2,
                          top: p[1] - size / 2,
                          width: size,
                          height: size,
                        }}
                      />
                    )
                  );
                })}
                {selCable.bends.map((b, i) => {
                  const size = 10 / view.z;
                  return (
                    <div
                      key={i}
                      data-bend={i}
                      title="Drag to move; double-click to remove"
                      className="absolute cursor-move rounded-sm border-2 border-in bg-panel"
                      style={{
                        left: b[0] - size / 2,
                        top: b[1] - size / 2,
                        width: size,
                        height: size,
                      }}
                      onDoubleClick={() =>
                        commit(
                          patchElement(doc, selCable.id, { bends: removeBend(selCable.bends, i) }),
                        )
                      }
                    />
                  );
                })}
              </>
            )}
            {draft && (
              <>
                <svg
                  className="pointer-events-none absolute top-0 left-0 overflow-visible"
                  width="1"
                  height="1"
                  aria-hidden="true"
                >
                  <line
                    x1={draft.from[0]}
                    y1={draft.from[1]}
                    x2={draft.x}
                    y2={draft.y}
                    stroke="var(--vt-in)"
                    strokeWidth={2 / view.z}
                    strokeDasharray={`${6 / view.z} ${4 / view.z}`}
                  />
                </svg>
                {draft.over &&
                  (() => {
                    const r = routes.endRect(draft.over);
                    return (
                      r && (
                        <>
                          <div
                            data-drop-target={draft.over.element}
                            className="pointer-events-none absolute rounded-[10px] border-2 border-in"
                            style={{ left: r.x - 3, top: r.y - 3, width: r.w + 6, height: r.h + 6 }}
                          />
                          <div
                            className="pointer-events-none absolute rounded bg-in px-1.5 text-white"
                            style={{ left: r.x, top: r.y - 22 / view.z, fontSize: 12 / view.z }}
                          >
                            Drop to connect
                          </div>
                        </>
                      )
                    );
                  })()}
              </>
            )}
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
          </MeasureProvider>
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
