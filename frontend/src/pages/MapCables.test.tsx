import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { Instance, MapDocument, NetworkMap } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { legendEntries } from "./maps/legend";
import { insertBend, removeVlan, setBoxPorts } from "./maps/editor/mapOps";

beforeEach(() => {
  mockSystemTheme(false);
  // jsdom has no PointerEvent: a mouse event carries the same button and coordinates.
  vi.stubGlobal("PointerEvent", MouseEvent);
  vi.stubGlobal(
    "matchMedia",
    vi.fn((media: string) => ({
      matches: media.includes("min-width"),
      media,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    })),
  );
});

const box = (id: string, x: number, label: string) => ({
  id,
  kind: "box" as const,
  x,
  y: 40,
  w: 200,
  h: 80,
  label,
  subtitle: "",
  icon: "server" as const,
  instance_id: null,
  show: { details: true, status: true, figures: true },
  figures: [],
  color: null,
  panel: null,
});

const cable = {
  id: "c1",
  kind: "cable" as const,
  from: { element: "a", port: null },
  to: { element: "b", port: null },
  speed: "1G" as const,
  style: "solid" as const,
  role: null,
  vlan: null,
  color: null,
  label: "",
  show_rate: false,
  bends: [],
};

const BOXES = [box("a", 40, "Alpha"), box("b", 400, "Beta")];
const doc = (...elements: unknown[]) =>
  ({ version: 1, vlans: [], elements: [...BOXES, ...elements] }) as unknown as MapDocument;

const stored = (d: MapDocument): NetworkMap => ({
  id: 1,
  name: "Home",
  position: 0,
  document: d,
  revision: 7,
  updated_at: "2026-01-01",
});

function pve(): Instance {
  return {
    id: "i1",
    type_id: "proxmox",
    name: "pve-01",
    site: null,
    tags: [],
    enabled: true,
    color: "violet",
    config: { host: "192.0.2.20" },
    secrets_set: {},
    status: { state: "up", checked_at: null, last_success_at: null, error: null },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function setup(start: MapDocument, extra: Record<string, (body: unknown) => Response> = {}) {
  const saved: MapDocument[] = [];
  const backend = fakeBackend({
    session: exampleMe("admin"),
    routes: {
      "GET /maps": () => json([{ id: 1, name: "Home", position: 0 }]),
      "GET /maps/1": () => json(stored(start)),
      "PUT /maps/1": (body) => {
        saved.push((body as { document: MapDocument }).document);
        return json(stored((body as { document: MapDocument }).document));
      },
      "GET /instances": () => json([pve()]),
      ...extra,
    },
  });
  return { backend, saved };
}

const viewport = () => screen.getByTestId("edit-viewport");
const el = (id: string) => viewport().querySelector(`[data-element="${id}"]`) as HTMLElement;
const select = (id: string) => {
  fireEvent.pointerDown(el(id), { button: 0 });
  fireEvent.pointerUp(el(id));
};

async function edit(d: MapDocument, extra?: Record<string, (body: unknown) => Response>) {
  const s = setup(d, extra);
  renderApp("/map", s.backend);
  fireEvent.click(await screen.findByRole("button", { name: "Edit map" }));
  return s;
}

const done = async (saved: MapDocument[]) => {
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await waitFor(() => expect(saved).toHaveLength(1));
  return saved[0] as MapDocument;
};

describe("cables in the editor", () => {
  it("draws a cable from one box to another and saves it", async () => {
    const { saved } = await edit(doc());
    fireEvent.click(screen.getByRole("button", { name: "Cable" }));
    fireEvent.pointerDown(el("a"), { button: 0 });
    fireEvent.pointerMove(el("b"), { clientX: 450, clientY: 80 });
    expect(await screen.findByText("Drop to connect")).toBeInTheDocument();
    fireEvent.pointerUp(el("b"), { clientX: 450, clientY: 80 });
    // The new cable is selected: its inspector is open.
    expect(await screen.findByRole("heading", { name: "Cable" })).toBeInTheDocument();
    const out = await done(saved);
    expect(out.elements.find((e) => e.kind === "cable")).toMatchObject({
      from: { element: "a", port: null },
      to: { element: "b", port: null },
      style: "solid",
    });
  });

  it("refuses a cable dropped on its own box, and Escape cancels", async () => {
    const { saved } = await edit(doc());
    fireEvent.click(screen.getByRole("button", { name: "Cable" }));
    fireEvent.pointerDown(el("a"), { button: 0 });
    fireEvent.pointerUp(el("a"));
    fireEvent.pointerDown(el("a"), { button: 0 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.pointerUp(el("b"));
    const out = await done(saved);
    expect(out.elements.some((e) => e.kind === "cable")).toBe(false);
  });

  it("adds a bend by dragging a selected cable, and it survives the save", async () => {
    const { saved } = await edit(doc(cable));
    const hit = () => viewport().querySelector('[data-hit="c1"]') as Element;
    fireEvent.pointerDown(hit(), { button: 0, clientX: 300, clientY: 50 });
    fireEvent.pointerUp(hit());
    // Selected now; drag the line.
    fireEvent.pointerDown(hit(), { button: 0, clientX: 300, clientY: 50 });
    fireEvent.pointerMove(viewport(), { clientX: 333, clientY: 211 });
    fireEvent.pointerUp(viewport());
    expect(viewport().querySelectorAll("[data-bend]")).toHaveLength(1);
    const out = await done(saved);
    const c = out.elements.find((e) => e.kind === "cable");
    expect(c?.kind === "cable" && c.bends).toEqual([[336, 208]]);
  });
});

describe("ports and VLANs in the editor", () => {
  it("saves a front panel built from port groups, with a VLAN, and draws its bar", async () => {
    const { saved } = await edit(doc());
    fireEvent.click(screen.getByRole("button", { name: "VLANs" }));
    fireEvent.click(screen.getByRole("button", { name: "Add VLAN" }));
    select("a");
    fireEvent.click(screen.getByRole("tab", { name: "Look and ports" }));
    fireEvent.click(screen.getByRole("button", { name: "Add RJ45 group" }));
    fireEvent.change(screen.getByLabelText("RJ45 1 untagged VLAN"), { target: { value: "1" } });
    expect(viewport().querySelector('[data-port="g1-1"] [data-vlan="1"]')).not.toBeNull();
    const out = await done(saved);
    const a = out.elements.find((e) => e.id === "a");
    expect(a?.kind === "box" && a.panel).toMatchObject({
      source: "manual",
      groups: [{ id: "g1", kind: "rj45", count: 4 }],
    });
    expect(a?.kind === "box" && a.panel?.ports).toHaveLength(4);
    expect(a?.kind === "box" && a.panel?.ports[0]).toMatchObject({
      id: "g1-1",
      vlans: { untagged: "1", tagged: [] },
    });
    expect(out.vlans).toEqual([
      { id: "1", name: "VLAN 1", color: expect.any(String), subnet: "", note: "" },
    ]);
  });

  it("clears a removed VLAN from ports and cables, and a removed port from cables", () => {
    const port = {
      id: "p1",
      group: "g1",
      n: 1,
      caption: "",
      vlans: { untagged: "10", tagged: ["20"] },
    };
    const withPorts = {
      ...doc({ ...cable, role: "access", vlan: "10", from: { element: "a", port: "p1" } }),
      vlans: [
        { id: "10", name: "LAN", color: "blue" },
        { id: "20", name: "IoT", color: "teal" },
      ],
    } as MapDocument;
    const strip = {
      source: "manual" as const,
      groups: [{ id: "g1", kind: "rj45", count: 1, rows: 1 }],
      ports: [port],
    };
    const d = setBoxPorts(withPorts, "a", strip as never);
    expect(legendEntries(d).vlans.map((v) => v.id)).toEqual(["10", "20"]);

    const cleared = removeVlan(d, "10");
    const a = cleared.elements.find((e) => e.id === "a");
    expect(cleared.vlans.map((v) => v.id)).toEqual(["20"]);
    expect(a?.kind === "box" && a.panel?.ports[0]?.vlans).toEqual({
      untagged: null,
      tagged: ["20"],
    });
    expect(cleared.elements.find((e) => e.kind === "cable")).toMatchObject({ vlan: null });

    const gone = setBoxPorts(d, "a", { ...strip, ports: [] } as never);
    expect(gone.elements.find((e) => e.kind === "cable")).toMatchObject({
      from: { element: "a", port: null },
    });
  });

  it("stops at 16 bends", () => {
    let bends: [number, number][] = [];
    for (let i = 0; i < 20; i++) bends = insertBend(bends, bends.length, i * 16, 0);
    expect(bends).toHaveLength(16);
  });
});

describe("Proxmox frame guests", () => {
  const frame = {
    id: "f1",
    kind: "frame" as const,
    x: 40,
    y: 200,
    w: 480,
    h: 280,
    label: "",
    instance_id: "i1",
    color: null,
    guests: { auto: true, hidden: [], order: [], manual: [] },
  };
  const guests = () =>
    json({
      result_type: "vector",
      series: ["110", "120"].map((vmid) => ({
        labels: { vmid, node: "pve", guest_type: "qemu" },
        values: [[0, "1"]],
      })),
      warnings: [],
    });

  it("hides a guest by vmid and adds a tile of its own", async () => {
    const { saved } = await edit(doc(frame), { "POST /query": guests });
    select("f1");
    fireEvent.click(await screen.findByLabelText("Show Guest 110"));
    fireEvent.click(screen.getByRole("button", { name: "Move Guest 120 up" }));
    fireEvent.click(screen.getByRole("button", { name: "Add tile" }));
    const out = await done(saved);
    const f = out.elements.find((e) => e.id === "f1");
    expect(f?.kind === "frame" && f.guests).toMatchObject({
      hidden: ["110"],
      order: ["120", "110"],
      manual: [{ id: "m1", label: "New tile" }],
    });
  });
});

describe("new map from my devices", () => {
  it("opens the blank map with the error when the devices can't be saved", async () => {
    let created = false;
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /maps": () => json(created ? [{ id: 2, name: "Everything", position: 0 }] : []),
        "POST /maps": () => {
          created = true;
          return json(
            { ...stored({ version: 1, vlans: [], elements: [] }), id: 2, revision: 1 },
            201,
          );
        },
        "PUT /maps/2": () => json({ detail: "document too large" }, 422),
        "GET /maps/2": () => json({ ...stored({ version: 1, vlans: [], elements: [] }), id: 2 }),
        "GET /instances": () => json([pve()]),
      },
    });
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "New map" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Everything" } });
    fireEvent.click(screen.getByLabelText(/Start with my devices/));
    fireEvent.click(screen.getByRole("button", { name: "Create and start editing" }));
    expect(await screen.findByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("document too large");
  });
});
