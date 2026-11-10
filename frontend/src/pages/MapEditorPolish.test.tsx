import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { MapBox, MapPanel } from "../api";
import { contentSized } from "./maps/boxFacts";
import { fitScale } from "./maps/fitScale";
import { emptyPanel, newGroup, regeneratePorts } from "./maps/panel";
import { boxPanel } from "./maps/panelGeometry";
import type { MeasuredBox } from "./maps/panelGeometry";
import { useInspectorWidth } from "./maps/editor/useInspectorWidth";

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
