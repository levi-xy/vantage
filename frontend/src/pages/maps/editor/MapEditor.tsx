// Edit mode of a map (admins only): a working copy of the document with undo and redo, a
// tool rail, the drawing, an inspector and the "Not on the map yet" list. Nothing is sent
// until Done: one PUT with the revision the editor opened with.

import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router";
import { ApiError } from "../../../api";
import type { Instance, MapCableEnd, MapDocument, NetworkMap } from "../../../api";
import { useApiClient } from "../../../app/session";
import { alertError, buttonPrimary, buttonSecondary } from "../../../components/styles";
import { errorText, useInstances, useIntegrations } from "../../devices/queries";
import { useMapLive } from "../mapLive";
import { useViewport } from "../useViewport";
import { DRAG_DEVICE_TYPE, EditCanvas } from "./EditCanvas";
import { Inspector } from "./Inspector";
import { INSPECTOR_MAX, INSPECTOR_MIN, useInspectorWidth } from "./useInspectorWidth";
import { makeCable, speedFromBps } from "./mapOps";
import { VlanPanel } from "./VlanPanel";
import {
  canAdd,
  clamp,
  GRID,
  iconFor,
  makeElement,
  MAX_COORD,
  MAX_ELEMENTS,
  notOnMap,
  patchElement,
  removeElements,
  snapTo,
  withElement,
} from "./model";
import type { NewKind } from "./model";
import { useDocHistory } from "./useDocHistory";

type Tool = { kind: "select" } | { kind: "cable" } | { kind: NewKind; instanceId?: string };

const TOOLS: { id: "select" | "device" | "cable" | NewKind; label: string }[] = [
  { id: "select", label: "Select" },
  { id: "device", label: "Device" },
  { id: "box", label: "Box" },
  { id: "frame", label: "Frame" },
  { id: "text", label: "Text" },
  { id: "table", label: "Table" },
  { id: "networks", label: "Networks" },
  { id: "legend", label: "Legend" },
  { id: "cable", label: "Cable" },
];

const STALE = "Someone else saved this map. Reload to see their version.";

function typing(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

export function MapEditor({
  map,
  onSaved,
  onClose,
  onReload,
}: {
  /** The map as it was when editing began; later refetches don't replace the working copy. */
  map: NetworkMap;
  onSaved: (saved: NetworkMap) => void;
  onClose: () => void;
  /** Throw the local copy away and show the stored map again. */
  onReload: () => void;
}) {
  const api = useApiClient();
  const h = useDocHistory(map.document);
  const { doc } = h;
  const instances = useInstances().data ?? [];
  const categories = useIntegrations().data ?? [];
  const live = useMapLive(doc);
  const [selectedRaw, setSelected] = useState<string[]>([]);
  const [tool, setTool] = useState<Tool>({ kind: "select" });
  const [inspectorW, setInspectorW] = useInspectorWidth();
  const [picking, setPicking] = useState(false);
  const [vlansOpen, setVlansOpen] = useState(false);
  const [snap, setSnap] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const viewport = useViewport(box, doc, map.document);

  // Undo can remove what is selected.
  const selected = selectedRaw.filter((id) => doc.elements.some((e) => e.id === id));

  const save = useMutation({
    mutationFn: () => api.saveMap(map.id, { document: doc, revision: map.revision }),
    onSuccess: onSaved,
  });
  const stale = save.error instanceof ApiError && save.error.status === 409;

  // Unsaved changes: ask before the tab closes or the app navigates away.
  const blocker = useBlocker(h.dirty && !save.isSuccess);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("Leave without saving? Your changes to this map will be lost.")) {
      blocker.proceed();
    } else blocker.reset();
  }, [blocker]);
  useEffect(() => {
    if (!h.dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [h.dirty]);

  const add = (kind: NewKind, x: number, y: number, instanceId?: string) => {
    if (!canAdd(doc)) {
      setNotice(`A map holds at most ${MAX_ELEMENTS} elements.`);
      return;
    }
    setNotice(null);
    const inst = instanceId ? instances.find((i) => i.id === instanceId) : undefined;
    const category = categories.find((t) => t.type_id === inst?.type_id)?.category;
    const at = (v: number) => (snap ? snapTo(v) : v);
    const el = makeElement(doc, kind, at(x), at(y), {
      instanceId: instanceId ?? null,
      icon: iconFor(category),
    });
    h.commit(withElement(doc, el));
    setSelected([el.id]);
    setTool({ kind: "select" });
  };

  /** A cable dragged from one end to another: speed from the port when it reports one. */
  const connect = (from: MapCableEnd, to: MapCableEnd) => {
    if (!canAdd(doc)) {
      setNotice(`A map holds at most ${MAX_ELEMENTS} elements.`);
      return;
    }
    const speedOf = (end: MapCableEnd) => {
      const owner = doc.elements.find((e) => e.id === end.element);
      const id = owner && "instance_id" in owner ? owner.instance_id : null;
      return speedFromBps(live.port(id, end.port)?.speed);
    };
    const cable = makeCable(doc, from, to, speedOf(from) ?? speedOf(to));
    if (!cable) {
      setNotice("A cable needs two different boxes.");
      return;
    }
    setNotice(null);
    h.commit(withElement(doc, cable));
    setSelected([cable.id]);
    setTool({ kind: "select" });
  };

  /** The middle of what is on screen, for click-to-place from the list. */
  const centre = (): [number, number] => {
    const { x, y, z } = viewport.view;
    const w = box.current?.clientWidth || 600;
    const h = box.current?.clientHeight || 400;
    return [(w / 2 - x) / z - 112, (h / 2 - y) / z - 44];
  };

  const removeSelected = () => {
    if (selected.length === 0) return;
    h.commit(removeElements(doc, selected));
    setSelected([]);
  };

  const nudge = (dx: number, dy: number) => {
    let next: MapDocument = doc;
    for (const id of selected) {
      const el = doc.elements.find((e) => e.id === id);
      if (el && el.kind !== "cable") {
        next = patchElement(next, id, {
          x: clamp(el.x + dx, 0, MAX_COORD),
          y: clamp(el.y + dy, 0, MAX_COORD),
        });
      }
    }
    h.commit(next, `nudge:${selected.join(",")}`);
  };

  const onKey = (e: KeyboardEvent) => {
    if (typing(e.target)) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) h.redo();
      else h.undo();
    } else if (mod && e.key.toLowerCase() === "y") {
      e.preventDefault();
      h.redo();
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeSelected();
    } else if (e.key === "Escape") {
      setTool({ kind: "select" });
      setSelected([]);
    } else if (e.key.startsWith("Arrow") && selected.length > 0) {
      e.preventDefault();
      const step = e.shiftKey ? GRID : 1;
      nudge(
        e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0,
        e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0,
      );
    }
  };
  useEffect(() => {
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const cancel = () => {
    if (!h.dirty || window.confirm("Discard your changes to this map?")) onClose();
  };

  const pickTool = (id: (typeof TOOLS)[number]["id"]) => {
    setPicking(id === "device" && !picking);
    if (id === "select") setTool({ kind: "select" });
    else if (id === "cable")
      setTool(tool.kind === "cable" ? { kind: "select" } : { kind: "cable" });
    else if (id !== "device") setTool({ kind: id });
    else setTool({ kind: "select" });
  };

  const armed = tool.kind !== "select";
  const notPlaced = notOnMap(doc, instances);
  const placeFromList = (inst: Instance) => {
    const [x, y] = centre();
    add("box", x, y, inst.id);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col p-3">
      <div
        role="toolbar"
        aria-label="Map editing"
        className="mb-3 flex shrink-0 flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2"
      >
        <span className="rounded bg-chip px-2 py-0.5 text-xs font-bold tracking-wide text-muted uppercase">
          Editing
        </span>
        <b className="mr-2 truncate">{map.name}</b>
        <button type="button" className={buttonSecondary} disabled={!h.canUndo} onClick={h.undo}>
          Undo
        </button>
        <button type="button" className={buttonSecondary} disabled={!h.canRedo} onClick={h.redo}>
          Redo
        </button>
        <button
          type="button"
          className={buttonSecondary}
          aria-pressed={vlansOpen}
          onClick={() => setVlansOpen(!vlansOpen)}
        >
          VLANs
        </button>
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" checked={snap} onChange={(e) => setSnap(e.target.checked)} />
          Snap to grid
        </label>
        <span className="ml-auto text-xs text-muted">
          Everyone sees the changes when you press Done.
        </span>
        <button type="button" className={buttonSecondary} onClick={cancel}>
          Cancel
        </button>
        <button
          type="button"
          className={buttonPrimary}
          disabled={save.isPending || stale}
          onClick={() => save.mutate()}
        >
          Done
        </button>
      </div>
      {save.error && (
        <div role="alert" className={`mb-3 flex flex-wrap items-center gap-3 ${alertError}`}>
          <span>{stale ? STALE : errorText(save.error)}</span>
          {stale && (
            <button type="button" className={buttonSecondary} onClick={onReload}>
              Reload
            </button>
          )}
        </div>
      )}
      {notice && (
        <p role="status" className="mb-3 text-sm text-muted">
          {notice}
        </p>
      )}
      <div
        className="grid min-h-[420px] flex-1 grid-rows-[minmax(0,1fr)] gap-3"
        style={{ gridTemplateColumns: `auto minmax(0,1fr) ${inspectorW}px` }}
      >
        <div
          role="toolbar"
          aria-label="Tools"
          aria-orientation="vertical"
          className="flex w-20 flex-col gap-1.5 overflow-y-auto"
        >
          {TOOLS.map((t) => {
            const active =
              t.id === "device"
                ? picking || (tool.kind === "box" && tool.instanceId !== undefined)
                : tool.kind === t.id;
            return (
              <button
                key={t.id}
                type="button"
                aria-pressed={active}
                className={`${buttonSecondary} ${active ? "border-in bg-inset text-ink" : ""}`}
                onClick={() => pickTool(t.id)}
              >
                {t.label}
              </button>
            );
          })}
        </div>
        <EditCanvas
          doc={doc}
          box={box}
          viewport={viewport}
          live={live}
          selected={selected}
          placing={armed && tool.kind !== "cable"}
          cabling={tool.kind === "cable"}
          snap={snap}
          onSelect={setSelected}
          onPlace={(x, y) => {
            if (tool.kind !== "select" && tool.kind !== "cable") {
              add(tool.kind, x, y, tool.instanceId);
            }
          }}
          onConnect={connect}
          commit={h.commit}
          onDropInstance={(id, x, y) => add("box", x, y, id)}
          preview={h.preview}
          commitFrom={h.commitFrom}
        />
        <div className="relative flex min-w-0 flex-col">
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the settings panel"
            aria-valuemin={INSPECTOR_MIN}
            aria-valuemax={INSPECTOR_MAX}
            aria-valuenow={inspectorW}
            tabIndex={0}
            className="absolute inset-y-0 -left-2 z-10 w-2 cursor-col-resize touch-none rounded hover:bg-in/30 focus-visible:bg-in/40 focus-visible:outline-none"
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture?.(e.pointerId);
              e.preventDefault();
            }}
            onPointerMove={(e) => {
              if (!e.currentTarget.hasPointerCapture?.(e.pointerId)) return;
              const right = e.currentTarget.parentElement?.getBoundingClientRect().right ?? 0;
              setInspectorW(right - e.clientX);
            }}
            onKeyDown={(e) => {
              if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
              e.preventDefault();
              setInspectorW(inspectorW + (e.key === "ArrowLeft" ? 16 : -16));
            }}
          />
          <aside
            aria-label="Inspector"
            className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto"
          >
            {picking && (
              <section
                aria-label="Pick a device"
                className="rounded-lg border border-line bg-panel p-3"
              >
                <h2 className="mb-2 text-sm font-semibold">Pick a device, then click the map</h2>
                <ul className="max-h-48 overflow-y-auto text-sm">
                  {instances.map((i) => (
                    <li key={i.id}>
                      <button
                        type="button"
                        className="w-full truncate rounded px-2 py-1 text-left hover:bg-inset focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
                        onClick={() => {
                          setTool({ kind: "box", instanceId: i.id });
                          setPicking(false);
                        }}
                      >
                        {i.name}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {armed && tool.kind === "box" && tool.instanceId !== undefined && (
              <p className="text-xs text-muted">
                Click the map to place{" "}
                {instances.find((i) => i.id === tool.instanceId)?.name ?? "the device"}.
              </p>
            )}
            {vlansOpen && (
              <VlanPanel
                doc={doc}
                edit={(next) => h.commit(next)}
                onClose={() => setVlansOpen(false)}
              />
            )}
            {tool.kind === "cable" && (
              <p className="text-xs text-muted">
                Drag from a port or a box to another one. Escape cancels.
              </p>
            )}
            <section aria-label="Settings" className="rounded-lg border border-line bg-panel p-3">
              <Inspector
                doc={doc}
                selected={selected}
                instances={instances}
                live={live}
                edit={(next) => h.commit(next)}
                patch={(id, p, key) => h.commit(patchElement(doc, id, p), key ? key : null)}
                remove={removeSelected}
              />
            </section>
            <section
              aria-label="Not on the map yet"
              className="rounded-lg border border-line bg-panel p-3"
            >
              <h2 className="mb-1 text-sm font-semibold">Not on the map yet</h2>
              <p className="mb-2 text-xs text-muted">Drag one in, or click it to place it.</p>
              {notPlaced.length === 0 ? (
                <p className="text-sm text-muted">Every device is on this map.</p>
              ) : (
                <ul className="max-h-56 overflow-y-auto text-sm">
                  {notPlaced.map((i) => (
                    <li key={i.id}>
                      <button
                        type="button"
                        draggable
                        onDragStart={(e) => e.dataTransfer.setData(DRAG_DEVICE_TYPE, i.id)}
                        className="w-full truncate rounded px-2 py-1 text-left hover:bg-inset focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
                        onClick={() => placeFromList(i)}
                      >
                        {i.name}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
