import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Instance, MapDocument, MapFrame, MapNetworks, MapTable } from "../api";
import { FrameView } from "./maps/FrameView";
import { NetworksView, TableView } from "./maps/MapElements";
import type { MapLive } from "./maps/mapLive";
import { LookAndPorts } from "./maps/editor/LookAndPorts";
import { emptyPanel, regeneratePorts } from "./maps/panel";
import { guestColumns } from "./maps/guests";
import type { GuestTile } from "./maps/guests";
import { legendEntries } from "./maps/legend";
import { makeElement } from "./maps/editor/model";
import { inflate } from "./maps/cableAvoid";
import { cablePath } from "./maps/geometry";
import { cableRoutes } from "./maps/cableRoutes";
import type { RouteCache } from "./maps/cableRoutes";

vi.mock("../cards/proxmox-guests/useProxmoxGuests", () => ({
  useProxmoxGuests: () => ({
    guests: [
      { vmid: "110", node: "pve", type: "lxc", name: "web", tags: [], running: true },
      { vmid: "120", node: "pve", type: "qemu", name: "db", tags: [], running: false },
      { vmid: "130", node: "pve", type: "lxc", name: "cache", tags: [], running: true },
    ],
    cpuBy: new Map([["110", 12]]),
    memBy: new Map([["110", 40]]),
  }),
}));

const PVE: Instance = {
  id: "i1",
  type_id: "proxmox",
  name: "pve-01",
  site: null,
  tags: [],
  enabled: true,
  color: "violet",
  config: { host: "192.0.2.10" },
  secrets_set: {},
  status: { state: "up", checked_at: null, last_success_at: null, error: null },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const live = (): MapLive => ({
  ready: true,
  instance: (id) => (id === "i1" ? PVE : undefined),
  state: (id) => (id === "i1" ? "up" : null),
  isDown: () => false,
  typeName: () => "Proxmox VE",
  ports: () => undefined,
  port: () => undefined,
  figure: () => undefined,
  figures: () => [],
  smartDisks: () => [],
});

const frame = (extra: Partial<MapFrame> = {}): MapFrame => ({
  id: "f1",
  kind: "frame",
  x: 0,
  y: 0,
  w: 900,
  h: 280,
  label: "",
  instance_id: "i1",
  color: null,
  guests: { auto: true, hidden: [], order: [], manual: [] },
  groups: [{ id: "g1", title: "Apps", members: ["g110"] }],
  bars: true,
  side: null,
  model: "",
  address: "",
  version: "",
  ...extra,
});

describe("frames", () => {
  it("sorts guests into titled groups and the rest into a trailing Other", () => {
    const { container } = render(<FrameView frame={frame()} live={live()} />);
    const groups = [...container.querySelectorAll("[data-group]")];
    expect(groups.map((g) => g.getAttribute("data-group"))).toEqual(["g1", "other"]);
    expect(groups[0]?.textContent).toContain("web");
    expect(groups[1]?.textContent).toContain("Other");
    expect(groups[1]?.textContent).toContain("db");
    expect(groups[1]?.textContent).toContain("Stopped");
    // Every guest is in a group: Other is not drawn.
    const all = frame({ groups: [{ id: "g1", title: "All", members: ["g110", "g120", "g130"] }] });
    const again = render(<FrameView frame={all} live={live()} />);
    expect(again.container.querySelector("[data-group='other']")).toBeNull();
  });

  it("shows CPU and memory bars only when `bars` is on", () => {
    const on = render(<FrameView frame={frame()} live={live()} />);
    expect(on.container.querySelectorAll("[data-bar]")).toHaveLength(2); // web: CPU and MEM
    on.unmount();
    const off = render(<FrameView frame={frame({ bars: false })} live={live()} />);
    expect(off.container.querySelectorAll("[data-bar]")).toHaveLength(0);
  });

  it("puts the device identity in a side column", () => {
    const side = { model: "Mini PC", address: "", version: "VE 8.2", sections: [] };
    const { container } = render(<FrameView frame={frame({ side })} live={live()} />);
    const col = container.querySelector("[data-side]") as HTMLElement;
    expect(col.textContent).toContain("Mini PC");
    expect(col.textContent).toContain("192.0.2.10"); // address from the device
  });

  it("splits tiles without groups into one untitled column", () => {
    const tile = (key: string): GuestTile => ({
      key,
      name: key,
      running: null,
      subtitle: "",
      ref: "",
      vmid: "",
      node: "",
    });
    expect(guestColumns([tile("m1")], [])).toEqual([{ id: "", title: "", tiles: [tile("m1")] }]);
  });
});

describe("networks, badges and legend", () => {
  const doc: MapDocument = {
    version: 1,
    vlans: [
      { id: "10", name: "LAN", color: "blue", subnet: "10.0.10.0/24", note: "trusted" },
      { id: "20", name: "Servers", color: "teal", subnet: "", note: "" },
    ],
    elements: [],
  };

  it("lists the map's VLANs with subnet and note", () => {
    const el: MapNetworks = { id: "n1", kind: "networks", x: 0, y: 0, w: 300, h: 100 };
    const { container } = render(<NetworksView el={el} doc={doc} />);
    const rows = container.querySelectorAll("[data-vlan-row]");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("VLAN 10");
    expect(rows[0]?.textContent).toContain("LAN");
    expect(rows[0]?.textContent).toContain("trusted");
    expect(rows[0]?.textContent).toContain("10.0.10.0/24");
  });

  it("renders the cells of badge columns as pills", () => {
    const el: MapTable = {
      id: "t1",
      kind: "table",
      x: 0,
      y: 0,
      w: 300,
      h: 100,
      title: "Open",
      columns: ["Port", "Proto"],
      rows: [{ cells: ["443", "TCP"], color: null }],
      badges: [1],
    };
    const { container } = render(<TableView el={el} />);
    expect([...container.querySelectorAll("[data-badge]")].map((b) => b.textContent)).toEqual([
      "TCP",
    ]);
  });

  it("makes a networks element and lists live-figure dots in the legend only where used", () => {
    expect(makeElement(doc, "networks", 0, 0).kind).toBe("networks");
    const entries = legendEntries({
      ...doc,
      elements: [makeElement(doc, "networks", 0, 0)],
    });
    expect(entries.networks).toBe(true);
    expect(entries.liveFigure).toBe(false);
    expect(entries.portVlans).toBe(false);
  });
});

describe("replacing ports from the Look and ports tab", () => {
  afterEach(() => vi.restoreAllMocks());

  const box = () => {
    const el = makeElement({ version: 1, vlans: [], elements: [] }, "box", 0, 0);
    if (el.kind !== "box") throw new Error("box expected");
    return {
      ...el,
      shape: "device" as const,
      panel: regeneratePorts({
        ...emptyPanel(),
        groups: [
          { id: "old", kind: "rj45", count: 2, rows: 1, order: "rows", first: 1, prefix: "" },
        ],
      }),
    };
  };
  const cabled = (): MapDocument => {
    const b = box();
    return {
      version: 1,
      vlans: [],
      elements: [
        b,
        { ...makeElement({ version: 1, vlans: [], elements: [b] }, "box", 400, 0), id: "b2" },
        {
          id: "c1",
          kind: "cable",
          from: { element: b.id, port: "old-1" },
          to: { element: "b2", port: null },
          speed: null,
          style: "solid",
          role: null,
          vlan: null,
          color: null,
          label: "",
          show_rate: false,
          bends: [],
        },
      ],
    };
  };

  const open = (edit: (d: MapDocument) => void) => {
    const doc = cabled();
    const b = doc.elements[0];
    if (b?.kind !== "box") throw new Error("box expected");
    render(
      <LookAndPorts
        doc={doc}
        box={b}
        instance={undefined}
        live={live()}
        patch={() => undefined}
        edit={edit}
      />,
    );
    expect(screen.getByText(/Replaces the current ports/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Start from"), { target: { value: "blank" } });
  };

  it("asks first when a cable would lose its port, and does nothing on No", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const edit = vi.fn();
    open(edit);
    expect(confirm).toHaveBeenCalledOnce();
    expect(edit).not.toHaveBeenCalled();
  });

  it("replaces the ports on Yes; the cable keeps its box and loses its port", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const edit = vi.fn();
    open(edit);
    const next = edit.mock.calls[0]?.[0] as MapDocument;
    const cable = next.elements.find((e) => e.kind === "cable");
    expect(cable?.kind === "cable" && cable.from).toEqual({ element: "b1", port: null });
  });
});

describe("cable routes", () => {
  const r = (x: number, y: number, w: number, h: number) => ({ x, y, w, h });
  type P = readonly [number, number];
  const crosses = (pts: readonly P[], o: ReturnType<typeof r>) =>
    pts.slice(1).some((p, i) => {
      const q = pts[i] as P;
      return (
        Math.max(p[0], q[0]) > o.x &&
        Math.min(p[0], q[0]) < o.x + o.w &&
        Math.max(p[1], q[1]) > o.y &&
        Math.min(p[1], q[1]) < o.y + o.h
      );
    });

  it("goes around a box in between and turns in a free gap, clear of the name tab", () => {
    const a = r(0, 0, 200, 80);
    const b = r(0, 400, 200, 80);
    const between = r(70, 200, 60, 80);
    const pts = cablePath(a, b, [], a, b, [inflate(between, true)]);
    expect(crosses(pts, between)).toBe(false);
    // Every turn stays above b's name tab.
    expect(Math.max(...pts.slice(0, -1).map((p) => p[1]))).toBeLessThanOrEqual(400 - 22);
  });
});

describe("groups on a device-source panel without own ports", () => {
  it("copies the live ports in first, so a cable keeps its port", () => {
    const el = makeElement({ version: 1, vlans: [], elements: [] }, "box", 0, 0);
    if (el.kind !== "box") throw new Error("box expected");
    const first = { ...el, shape: "device" as const, panel: emptyPanel("device") };
    const doc: MapDocument = {
      version: 1,
      vlans: [],
      elements: [
        first,
        { ...makeElement({ version: 1, vlans: [], elements: [first] }, "box", 400, 0), id: "b2" },
        {
          id: "c1",
          kind: "cable",
          from: { element: first.id, port: "eth0" },
          to: { element: "b2", port: null },
          speed: null,
          style: "solid",
          role: null,
          vlan: null,
          color: null,
          label: "",
          show_rate: false,
          bends: [],
        },
      ],
    };
    const device = ["eth0", "eth1"].map((id, order) => ({
      id,
      name: id,
      state: null,
      ref: null,
      order,
      physical: true,
    }));
    const edit = vi.fn();
    render(
      <LookAndPorts
        doc={doc}
        box={first}
        instance={undefined}
        live={{ ...live(), ports: () => device }}
        patch={() => undefined}
        edit={edit}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: /^Add .* group$/i })[0] as HTMLElement);
    const next = edit.mock.calls[0]?.[0] as MapDocument;
    const cable = next.elements.find((e) => e.kind === "cable");
    expect(cable?.kind === "cable" && cable.from.port).toBe("eth0");
  });
});

describe("routing a big map", () => {
  it("routes 200 cables among 300 boxes quickly", () => {
    const elements: MapDocument["elements"] = [];
    for (let i = 0; i < 300; i++) {
      elements.push(
        makeElement(
          { version: 1, vlans: [], elements },
          "box",
          (i % 20) * 300,
          Math.floor(i / 20) * 200,
        ),
      );
    }
    for (let i = 0; i < 200; i++) {
      elements.push({
        id: `k${i}`,
        kind: "cable",
        from: { element: `b${i + 1}`, port: null },
        to: { element: `b${((i * 7 + 40) % 300) + 1}`, port: null },
        speed: null,
        style: "solid",
        role: null,
        vlan: null,
        color: null,
        label: "link",
        show_rate: false,
        bends: [],
      });
    }
    const big: MapDocument = { version: 1, vlans: [], elements };
    const all = () => elements.filter((e) => e.kind === "cable");
    const cache: RouteCache = new Map();
    const first = cableRoutes(big, live(), cache);
    for (const e of all()) if (e.kind === "cable") first.points(e);
    // A second pass with nothing changed reuses every route.
    const t1 = performance.now();
    const again = cableRoutes(big, live(), cache);
    for (const e of all()) if (e.kind === "cable") again.points(e);
    expect(performance.now() - t1).toBeLessThan(1500);
  });

  it("routes the first time in reasonable time", () => {
    const elements: MapDocument["elements"] = [];
    for (let i = 0; i < 300; i++) {
      elements.push(
        makeElement(
          { version: 1, vlans: [], elements },
          "box",
          (i % 20) * 300,
          Math.floor(i / 20) * 200,
        ),
      );
    }
    for (let i = 0; i < 200; i++) {
      elements.push({
        id: `k${i}`,
        kind: "cable",
        from: { element: `b${i + 1}`, port: null },
        to: { element: `b${((i * 7 + 40) % 300) + 1}`, port: null },
        speed: null,
        style: "solid",
        role: null,
        vlan: null,
        color: null,
        label: "",
        show_rate: false,
        bends: [],
      });
    }
    const t0 = performance.now();
    const routes = cableRoutes({ version: 1, vlans: [], elements }, live());
    for (const e of elements) if (e.kind === "cable") routes.points(e);
    expect(performance.now() - t0).toBeLessThan(3000);
  });
});
