import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { Instance, MapBox, MapSection } from "../api";
import { BoxView } from "./maps/BoxView";
import { boxIdentity } from "./maps/boxFacts";
import { emptyPanel } from "./maps/panel";
import { cablePath } from "./maps/geometry";
import { withHeights } from "./maps/measure";
import type { MapLive } from "./maps/mapLive";
import { SectionsEditor } from "./maps/editor/BoxContentSettings";
import {
  addItem,
  addSection,
  moveSection,
  removeItem,
  removeSection,
} from "./maps/editor/sectionOps";

const INSTANCE: Instance = {
  id: "i1",
  type_id: "pfsense",
  name: "edge-fw",
  site: null,
  tags: [],
  enabled: true,
  color: "violet",
  config: { host: "192.0.2.1" },
  secrets_set: {},
  status: { state: "up", checked_at: null, last_success_at: null, error: null },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/** A MapLive with one up device and the given live figure values. */
function fakeLive(figures: Record<string, string> = {}): MapLive {
  return {
    ready: true,
    instance: (id) => (id === "i1" ? INSTANCE : undefined),
    state: (id) => (id === "i1" ? "up" : null),
    isDown: () => false,
    typeName: () => "pfSense",
    ports: () => undefined,
    port: () => undefined,
    figure: (_id, key) => figures[key],
    figures: () => [],
    smartDisks: () => [],
  };
}

const item = (id: string, extra: Partial<MapSection["items"][number]> = {}) => ({
  id,
  label: id,
  value: "",
  sub: "",
  icon: null,
  color: null,
  figure: null,
  ...extra,
});

const make = (extra: Partial<MapBox> = {}): MapBox => ({
  id: "b1",
  kind: "box",
  x: 10,
  y: 20,
  w: 300,
  h: 80,
  label: "",
  subtitle: "",
  shape: "card",
  role: "router",
  icon: "firewall",
  model: "",
  address: "",
  version: "",
  instance_id: "i1",
  show: { details: true, status: true, figures: false },
  figures: [],
  sections: [],
  color: null,
  panel: null,
  nas: null,
  ...extra,
});

const draw = (box: MapBox, live = fakeLive()) =>
  render(<BoxView box={box} live={live} vlans={new Map()} />);

describe("box shapes", () => {
  it("draws the shape the box asks for", () => {
    for (const shape of ["card", "cloud", "device", "nas", "ap"] as const) {
      const { container, unmount } = draw(make({ shape }));
      expect(container.querySelector("[data-element='b1']")?.getAttribute("data-shape")).toBe(
        shape,
      );
      unmount();
    }
    // Looks: a rack unit has ears, a desktop unit has feet, a flat one has neither.
    const panel = (look: "rack" | "desktop" | "flat") => ({ ...emptyPanel("manual"), look });
    const rack = draw(make({ shape: "device", panel: panel("rack") }));
    expect(rack.container.querySelectorAll("[data-ear]")).toHaveLength(2);
    rack.unmount();
    const desktop = draw(make({ shape: "device", panel: panel("desktop") }));
    expect(desktop.container.querySelectorAll("[data-foot]")).toHaveLength(2);
    desktop.unmount();
    const flat = draw(make({ shape: "device", panel: panel("flat") }));
    expect(flat.container.querySelector("[data-ear],[data-foot]")).toBeNull();
  });

  it("puts name, role and status on the tab and fills the identity from the device", () => {
    const { container } = draw(make({ version: "2.7.2" }));
    const tab = container.querySelector("[data-tab]") as HTMLElement;
    expect(tab.textContent).toContain("edge-fw");
    expect(tab.textContent).toContain("· router");
    expect(tab.textContent).toContain("UP");
    expect(screen.getByText("pfSense")).toBeTruthy(); // model, from the integration
    expect(screen.getByText("192.0.2.1")).toBeTruthy(); // address, from the config
    expect(screen.getByText("2.7.2")).toBeTruthy();
    expect(boxIdentity(make({ model: "Typed" }), INSTANCE, fakeLive()).model).toBe("Typed");
  });

  it("shows a figure tile with a live dot, and hides it without data (never a dash)", () => {
    const sections: MapSection[] = [
      {
        id: "s1",
        title: "Hardware",
        kind: "tiles",
        columns: 2,
        items: [
          item("cpu", { label: "CPU", figure: "cpu" }),
          item("mem", { label: "Memory", figure: "memory" }),
          item("note", { label: "Note", value: "typed" }),
        ],
      },
    ];
    const { container } = draw(make({ sections }), fakeLive({ cpu: "0.9 %" }));
    const cpu = container.querySelector("[data-item='cpu']") as HTMLElement;
    expect(cpu.textContent).toContain("0.9 %");
    expect(cpu.querySelector("[data-live]")).not.toBeNull();
    expect(container.querySelector("[data-item='mem']")).toBeNull();
    expect(container.textContent).not.toContain("–");
    expect(container.querySelector("[data-item='note']")?.querySelector("[data-live]")).toBeNull();
  });

  it("sizes to its content and cuts long text with a hover title", () => {
    const long = "x".repeat(60);
    const { container } = draw(make({ model: long, h: 500 }));
    const root = container.querySelector("[data-element='b1']") as HTMLElement;
    expect(root.style.width).toBe("300px");
    expect(root.style.height).toBe(""); // the stored h is ignored
    const line = screen.getByText(long);
    expect(line.className).toContain("truncate");
    expect(line.getAttribute("title")).toBe(long);
  });

  it("anchors cables at the measured edge of a content-sized box", () => {
    const doc = {
      version: 1 as const,
      vlans: [],
      elements: [make({ h: 80 })],
    };
    const shown = withHeights(doc, new Map([["b1", 140]]));
    const el = shown.elements[0] as MapBox;
    expect(el.h).toBe(140);
    expect(doc.elements[0]?.h).toBe(80); // the saved document is untouched
    const below = { x: 10, y: 400, w: 300, h: 50 };
    const path = cablePath({ x: el.x, y: el.y, w: el.w, h: el.h }, below, []);
    expect(path[0]).toEqual([160, 160]); // bottom edge: y 20 + measured 140
  });
});

describe("sections editor", () => {
  it("adds, removes and reorders sections and items within the caps", () => {
    let s = addSection([]);
    s = addSection(s);
    expect(s.map((x) => x.id)).toEqual(["s1", "s2"]);
    s = moveSection(s, 1, -1);
    expect(s.map((x) => x.id)).toEqual(["s2", "s1"]);
    s = addItem(s, "s1");
    s = addItem(s, "s1");
    expect(s[1]?.items.map((i) => i.id)).toEqual(["i1", "i2"]);
    s = removeItem(s, "s1", "i1");
    expect(s[1]?.items.map((i) => i.id)).toEqual(["i2"]);
    s = removeSection(s, "s2");
    expect(s.map((x) => x.id)).toEqual(["s1"]);
    let many = [] as MapSection[];
    for (let n = 0; n < 6; n++) many = addSection(many);
    expect(many).toHaveLength(4);
  });

  it("edits through the form", () => {
    function Harness() {
      const [sections, setSections] = useState<MapSection[]>([]);
      return (
        <>
          <SectionsEditor sections={sections} instance={INSTANCE} set={setSections} />
          <output data-testid="json">{JSON.stringify(sections)}</output>
        </>
      );
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Add section" }));
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));
    fireEvent.change(screen.getByLabelText("Item 1 label"), { target: { value: "CPU" } });
    fireEvent.change(screen.getByLabelText("Item 1 live figure"), { target: { value: "cpu" } });
    const json = JSON.parse(screen.getByTestId("json").textContent ?? "[]") as MapSection[];
    expect(json[0]?.items[0]).toMatchObject({ label: "CPU", figure: "cpu" });
    // The figure picker offers this device's figures.
    const picker = screen.getByLabelText("Item 1 live figure");
    expect(within(picker).getByText("Live: States")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove item 1" }));
    expect(screen.queryByLabelText("Item 1 label")).toBeNull();
  });
});
