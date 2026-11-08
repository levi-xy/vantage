import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MapBox, MapPanel, MapPort, MapPortGroup } from "../api";
import { BoxView } from "./maps/BoxView";
import { portCables } from "./maps/cableLive";
import type { DevicePort } from "./maps/devicePorts";
import type { MapLive } from "./maps/mapLive";
import { emptyPanel, fromDevice, newPort, regeneratePorts } from "./maps/panel";
import { boxPanel, panelGeometry } from "./maps/panelGeometry";
import { bays } from "./maps/smartDisks";
import type { SmartDisk } from "./maps/smartDisks";
import { applyPreset, PRESETS } from "./maps/editor/presets";

const group = (id: string, kind: MapPortGroup["kind"], patch: Partial<MapPortGroup> = {}) => ({
  id,
  kind,
  count: 1,
  rows: 1 as const,
  odd_top: false,
  first: 1,
  prefix: "",
  ...patch,
});

function panel(groups: MapPortGroup[], patch: Partial<MapPanel> = {}): MapPanel {
  return regeneratePorts({ ...emptyPanel(), groups, ...patch });
}

const make = (extra: Partial<MapBox> = {}): MapBox => ({
  id: "b1",
  kind: "box",
  x: 10,
  y: 20,
  w: 560,
  h: 120,
  label: "Switch",
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

function fakeLive(ports: DevicePort[] = [], disks: SmartDisk[] = []): MapLive {
  return {
    ready: true,
    instance: () => undefined,
    state: () => null,
    isDown: () => false,
    typeName: () => "",
    ports: () => ports,
    port: (_id, portId) => ports.find((p) => p.id === portId),
    smartDisks: () => disks,
    figure: () => undefined,
    figures: () => [],
  };
}

const draw = (box: MapBox, live = fakeLive(), cabled?: ReadonlyMap<string, string>) =>
  render(<BoxView box={box} live={live} vlans={new Map()} cabled={cabled} />);

describe("front panel jacks", () => {
  it("draws RJ45 jacks, cages, console and USB, numbered from the group's first number", () => {
    const p = panel([
      group("g1", "rj45", { count: 2, first: 5 }),
      group("g2", "sfp_plus", {}),
      group("g3", "qsfp"),
      group("g4", "console"),
      group("g5", "usb"),
    ]);
    const { container } = draw(make({ panel: p }));
    const shape = (id: string) =>
      container.querySelector(`[data-port="${id}"]`)?.getAttribute("data-jack");
    expect([shape("g1-1"), shape("g2-1"), shape("g3-1"), shape("g4-1"), shape("g5-1")]).toEqual([
      "rj45",
      "cage",
      "cage",
      "console",
      "usb",
    ]);
    expect(container.querySelector('[data-port="g1-2"] [data-number]')?.textContent).toBe("6");
    // The RJ45 drawing has eight gold contacts.
    expect(container.querySelectorAll('[data-port="g1-1"] svg rect[fill="#c9a227"]')).toHaveLength(
      8,
    );
  });

  it("draws the top row of an odd_top group upside down, odd numbers on top", () => {
    const p = panel([group("g1", "rj45", { count: 4, rows: 2, odd_top: true })]);
    const { container } = draw(make({ panel: p }));
    const flipped = (n: number) =>
      container.querySelector(`[data-port="g1-${n}"]`)?.hasAttribute("data-flip");
    expect([flipped(1), flipped(2), flipped(3), flipped(4)]).toEqual([true, false, true, false]);
    // Top row: number below the jack (after it in the cell); bottom row: number above.
    const geo = panelGeometry(p, undefined, 500);
    const [one, two] = geo?.jacks ?? [];
    expect(one && two && one.y < two.y).toBe(true);
    expect(one && one.numberY > one.jackY).toBe(true);
    expect(two && two.numberY < two.jackY).toBe(true);
  });

  it("lights the link LED red only on a port with a cable, green when up", () => {
    const p = panel([group("g1", "rj45", { count: 3 })]);
    const device = (id: string, state: "up" | "down"): DevicePort => ({
      id,
      name: id,
      state,
      ref: null,
      order: 0,
    });
    const box = make({
      panel: {
        ...p,
        ports: p.ports.map((port, i) => ({ ...port, interface: `eth${i}` })),
      },
    });
    const live = fakeLive([device("eth0", "up"), device("eth1", "down"), device("eth2", "down")]);
    const { container } = draw(box, live, new Map([["g1-2", "Other"]]));
    const led = (id: string) =>
      container.querySelector(`[data-port="${id}"] svg rect[x="2"][y="17"]`)?.getAttribute("fill");
    expect(led("g1-1")).toBe("var(--vt-ok)");
    expect(led("g1-2")).toBe("var(--vt-crit)");
    expect(led("g1-3")).toBe("var(--vt-port-off)");
  });

  it("takes captions from the cable's other end, else the typed one, or none", () => {
    const base = panel([group("g1", "rj45", { count: 2 })]);
    const typed = {
      ...base,
      ports: base.ports.map((p) => ({ ...p, caption: p.n === 1 ? "desk" : "" })),
    };
    const cabled = new Map([["g1-1", "Emby"]]);
    const text = (p: MapPanel) =>
      draw(make({ panel: p }), fakeLive(), cabled).container.querySelector('[data-port="g1-1"]')
        ?.textContent;
    expect(text({ ...typed, captions: "cables" })).toContain("Emby");
    expect(text({ ...typed, captions: "typed" })).toContain("desk");
    expect(text({ ...typed, captions: "off" })).not.toContain("desk");
    // A port nobody cabled falls back to its typed caption in "cables" mode.
    expect(
      draw(make({ panel: { ...typed, captions: "cables" } })).container.querySelector(
        '[data-port="g1-1"]',
      )?.textContent,
    ).toContain("desk");
  });

  it("shows a hover card with the number and VLANs", () => {
    const p = panel([group("g1", "rj45")]);
    const box = make({
      panel: {
        ...p,
        ports: [{ ...(p.ports[0] as MapPort), vlans: { untagged: "10", tagged: [] } }],
      },
    });
    const { container } = draw(box);
    fireEvent.mouseEnter(container.querySelector('[data-port="g1-1"]') as Element);
    const card = container.querySelector("[data-port-card]");
    expect(card?.textContent).toContain("RJ45 1");
    expect(card?.textContent).toContain("Untagged 10");
  });

  it("gives cables the jack's rectangle in the box", () => {
    const p = panel([group("g1", "rj45", { count: 2 })]);
    const box = make({ panel: p });
    const rects = boxPanel(box, undefined)?.rects;
    const a = rects?.get("g1-1");
    const b = rects?.get("g1-2");
    expect(a && b && b.x > a.x && a.y === b.y).toBe(true);
    expect(a && a.x >= box.x && a.y >= box.y).toBe(true);
    // The cable's other end is named by the box at the far end of the cable.
    const doc = {
      version: 1 as const,
      vlans: [],
      elements: [
        box,
        make({ id: "b2", label: "Other" }),
        {
          id: "c1",
          kind: "cable" as const,
          from: { element: "b1", port: "g1-1" },
          to: { element: "b2", port: null },
          speed: null,
          style: "solid" as const,
          role: null,
          vlan: null,
          color: null,
          label: "",
          show_rate: false,
          bends: [] as [number, number][],
        },
      ],
    };
    expect(portCables(doc, fakeLive()).get("b1")?.get("g1-1")).toBe("Other");
  });
});

describe("port groups and presets", () => {
  it("keeps ids, captions and VLANs of numbers that still exist when ports are regenerated", () => {
    const p = panel([group("g1", "rj45", { count: 4 })]);
    const edited: MapPanel = {
      ...p,
      ports: p.ports.map((port) =>
        port.n === 2
          ? { ...port, caption: "NAS", vlans: { untagged: "30", tagged: ["10"] } }
          : port,
      ),
    };
    const smaller = regeneratePorts({ ...edited, groups: [group("g1", "rj45", { count: 3 })] });
    expect(smaller.ports.map((x) => x.id)).toEqual(["g1-1", "g1-2", "g1-3"]);
    expect(smaller.ports[1]).toMatchObject({ caption: "NAS", vlans: { untagged: "30" } });
    const bigger = regeneratePorts({ ...edited, groups: [group("g1", "rj45", { count: 6 })] });
    expect(bigger.ports).toHaveLength(6);
    expect(bigger.ports[1]).toMatchObject({ id: "g1-2", caption: "NAS" });
  });

  it("copies a preset into the box, where it stays editable", () => {
    const preset = PRESETS.find((x) => x.id === "generic-24");
    if (!preset) throw new Error("preset missing");
    const old = panel([group("g1", "rj45", { count: 8 })]);
    const first = old.ports[0] as MapPort;
    const box = make({
      panel: { ...old, ports: [{ ...first, caption: "keep" }, ...old.ports.slice(1)] },
    });
    const copied = applyPreset(box, preset);
    const next = copied.panel as MapPanel;
    expect(next.groups).toHaveLength(1);
    expect(next.ports).toHaveLength(24);
    expect(next.ports[0]).toMatchObject({ id: "g1-1", caption: "keep" });
    // Edit it afterwards: drop to 12 ports, the rest stays.
    const edited = regeneratePorts({
      ...next,
      groups: [{ ...(next.groups[0] as MapPortGroup), count: 12 }],
    });
    expect(edited.ports).toHaveLength(12);
    expect(edited.ports[0]?.caption).toBe("keep");
  });

  it("builds groups from a device's interfaces and keeps the ports cables name", () => {
    const device: DevicePort[] = ["ether1", "ether2", "sfp-sfpplus1"].map((id, order) => ({
      id,
      name: id,
      state: "up",
      ref: null,
      order,
    }));
    const read = fromDevice(device, [newPort("ether2", { caption: "x", interface: "ether2" })]);
    expect(read.groups.map((g) => g.kind)).toEqual(["rj45", "sfp_plus"]);
    expect(read.groups[0]?.prefix).toBe("ether");
    expect(read.ports.find((p) => p.interface === "ether2")).toMatchObject({
      id: "ether2",
      caption: "x",
    });
  });
});

describe("NAS drive bays", () => {
  const disk = (device: string, extra: Partial<SmartDisk> = {}): SmartDisk => ({
    device,
    serial: `SN-${device}`,
    model: "Example",
    healthy: true,
    temperature: 38,
    size: 16e12,
    ...extra,
  });

  it("gives each bay its state: healthy, warm, failing, unknown, empty", () => {
    const list = bays(
      {
        bays: 5,
        bay_disks: [
          { bay: 1, label: "", disk: "sda" },
          { bay: 2, label: "", disk: "SN-sdb" },
          { bay: 3, label: "", disk: "sdc" },
          { bay: 4, label: "spare", disk: "" },
        ],
      },
      [disk("sda"), disk("sdb", { temperature: 52 }), disk("sdc", { healthy: false })],
    );
    expect(list.map((b) => b.state)).toEqual(["healthy", "warm", "failing", "unknown", "empty"]);
  });

  it("draws the trays with size and temperature from the matched disk", () => {
    const box = make({
      shape: "nas",
      nas: { bays: 2, bay_disks: [{ bay: 1, label: "", disk: "sda" }] },
    });
    const { container } = draw(box, fakeLive([], [disk("sda")]));
    expect(container.querySelector('[data-bay="1"]')).toHaveAttribute("data-bay-state", "healthy");
    expect(container.querySelector('[data-bay="2"]')).toHaveAttribute("data-bay-state", "empty");
    expect(container.querySelector("[data-bays]")?.textContent).toContain("38");
  });
});
