import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { MapBox, MapCable, MapDocument, MapPanel } from "../api";
import { contentSized } from "./maps/boxFacts";
import { cableRoutes } from "./maps/cableRoutes";
import { fitScale } from "./maps/fitScale";
import type { MapLive } from "./maps/mapLive";
import { emptyPanel, newGroup, regeneratePorts } from "./maps/panel";
import { boxPanel } from "./maps/panelGeometry";
import type { MeasuredBox } from "./maps/panelGeometry";
import { useInspectorWidth } from "./maps/editor/useInspectorWidth";

const live = {
  ready: true,
  instance: () => undefined,
  state: () => null,
  isDown: () => false,
  typeName: () => "",
  ports: () => [],
  port: () => undefined,
  smartDisks: () => [],
  figure: () => undefined,
  figures: () => [],
} as MapLive;

const panel: MapPanel = regeneratePorts({
  ...emptyPanel(),
  groups: [newGroup([], "rj45", { count: 4 })],
});

const box = (id: string, x: number, y: number, extra: Partial<MapBox> = {}): MapBox => ({
  id,
  kind: "box",
  x,
  y,
  w: 300,
  h: 150,
  label: id,
  subtitle: "",
  shape: "device",
  role: "",
  icon: "switch",
  model: "",
  address: "",
  version: "",
  instance_id: null,
  show: { details: true, status: true, figures: false },
  figures: [],
  sections: [],
  color: null,
  panel: null,
  nas: null,
  ...extra,
});

const cable = (portA: string | null): MapCable => ({
  id: "c1",
  kind: "cable",
  from: { element: "a", port: portA },
  to: { element: "b", port: null },
  speed: null,
  style: "solid",
  role: null,
  vlan: null,
  color: null,
  label: "",
  show_rate: false,
  bends: [],
});

describe("cable to a jack", () => {
  const a = box("a", 0, 0, { panel });
  const b = box("b", 0, 400);
  const doc = (c: MapCable): MapDocument => ({ version: 1, vlans: [], elements: [a, b, c] });

  it("starts on the jack and leaves through the panel's open side", () => {
    const routes = cableRoutes(doc(cable("g1-2")), live);
    const jack = boxPanel(a, undefined)?.rects.get("g1-2");
    const pts = routes.points(cable("g1-2"));
    expect(jack && pts).toBeTruthy();
    if (!jack || !pts) return;
    // The first point is on the jack, inside the box; the next is below the box.
    expect(pts[0]?.[0]).toBe(jack.x + jack.w / 2);
    expect(pts[0]?.[1]).toBeGreaterThan(jack.y);
    expect(pts[0]?.[1]).toBeLessThan(a.y + a.h);
    expect(pts[1]?.[1]).toBeGreaterThan(a.y + a.h);
    // The part inside the box is drawn above it.
    expect(routes.inside(cable("g1-2"))).toEqual([{ x: 0, y: 0, w: 300, h: 150 }]);
  });

  it("keeps the box edge for a cable without a port", () => {
    const routes = cableRoutes(doc(cable(null)), live);
    expect(routes.inside(cable(null))).toEqual([]);
  });
});

describe("boxes taller than their content", () => {
  it("scales content up to fill, never down", () => {
    const natural = { w: 100, h: 100 };
    expect(fitScale({ w: 400, h: 300 }, natural)).toBe(3); // the tighter side decides: no cropping
    expect(fitScale({ w: 150, h: 900 }, natural)).toBe(1.5);
    expect(fitScale({ w: 90, h: 90 }, natural)).toBe(1);
    expect(fitScale({ w: 101, h: 101 }, natural)).toBe(1);
  });

  it("is height-resizable except clouds", () => {
    expect(contentSized(box("a", 0, 0))).toBe(false);
    expect(contentSized(box("a", 0, 0, { shape: "cloud" }))).toBe(true);
  });

  it("moves jack rectangles with the content scale", () => {
    const plain = box("a", 0, 0, { panel });
    const scaled: MeasuredBox = { ...plain, panelOrigin: { x: 20, y: 50, s: 2 } };
    const one = boxPanel({ ...plain, panelOrigin: { x: 20, y: 50 } } as MeasuredBox, undefined);
    const two = boxPanel(scaled, undefined);
    const [r1, r2] = [one?.rects.get("g1-1"), two?.rects.get("g1-1")];
    expect(r1 && r2 && r2.w).toBe(r1 ? r1.w * 2 : -1);
  });
});

describe("inspector width", () => {
  beforeEach(() => localStorage.clear());

  it("stays within its limits and is remembered", () => {
    const { result } = renderHook(() => useInspectorWidth());
    act(() => result.current[1](5000));
    expect(result.current[0]).toBe(640);
    act(() => result.current[1](10));
    expect(result.current[0]).toBe(280);
    expect(localStorage.getItem("vantage.mapInspectorWidth")).toBe("280");
    expect(renderHook(() => useInspectorWidth()).result.current[0]).toBe(280);
  });
});

describe("jack with a jack in the way", () => {
  it("leaves beside a top-row jack of a two-row panel, not through the jack below", () => {
    const two: MapPanel = regeneratePorts({
      ...emptyPanel(),
      groups: [newGroup([], "rj45", { count: 4, rows: 2, order: "odd_bottom" })],
    });
    const a = box("a", 0, 0, { panel: two });
    const doc: MapDocument = {
      version: 1,
      vlans: [],
      elements: [a, box("b", 0, 400), cable("g1-2")],
    };
    const jacks = boxPanel(a, undefined)?.rects;
    const top = [...(jacks?.values() ?? [])].reduce((m, r) => (r.y < m.y ? r : m));
    const id = [...(jacks?.entries() ?? [])].find(([, r]) => r === top)?.[0] ?? "";
    const c = { ...cable(id) };
    const pts = cableRoutes({ ...doc, elements: [a, box("b", 0, 400), c] }, live).points(c);
    // The second point is to the right of the jack (the gap between columns).
    expect(pts?.[1]?.[0]).toBeGreaterThan(top.x + top.w);
  });
});
