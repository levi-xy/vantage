import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Instance, MapDocument, NetworkMap } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { alignTo, devicesDocument, removeElements } from "./maps/editor/model";

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

function instance(id: string, name: string, site: string | null = null): Instance {
  return {
    id,
    type_id: "ping_check",
    name,
    site,
    tags: [],
    enabled: true,
    color: "violet",
    config: { host: "192.0.2.10" },
    secrets_set: {},
    status: { state: "up", checked_at: null, last_success_at: null, error: null },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

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

const DOC = {
  version: 1,
  vlans: [],
  elements: [box("a", 40, "Alpha"), box("b", 400, "Beta"), cable],
} as unknown as MapDocument;

const stored = (doc: MapDocument): NetworkMap => ({
  id: 1,
  name: "Home",
  position: 0,
  document: doc,
  revision: 7,
  updated_at: "2026-01-01",
});

function setup(
  role: "admin" | "viewer",
  put: (body: unknown) => Response = () => json(stored(DOC)),
  doc: MapDocument = DOC,
) {
  const saved: { document: MapDocument; revision: number }[] = [];
  const backend = fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /maps": () => json([{ id: 1, name: "Home", position: 0 }]),
      "GET /maps/1": () => json(stored(doc)),
      "PUT /maps/1": (body) => {
        saved.push(body as { document: MapDocument; revision: number });
        return put(body);
      },
      "GET /instances": () => json([instance("i1", "edge-fw", "Lab"), instance("i2", "core-sw")]),
    },
  });
  return { backend, saved };
}

const viewport = () => screen.getByTestId("edit-viewport");
const kinds = (d: MapDocument) => d.elements.map((e) => e.id);

describe("map editor", () => {
  it("shows no Edit button to a viewer", async () => {
    renderApp("/map", setup("viewer").backend);
    await screen.findByRole("region", { name: "Network map" });
    expect(screen.queryByRole("button", { name: "Edit map" })).not.toBeInTheDocument();
  });

  it("places a box and saves it once with the revision", async () => {
    const { backend, saved } = setup("admin");
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Edit map" }));
    fireEvent.click(screen.getByRole("button", { name: "Box" }));
    fireEvent.pointerDown(viewport(), { button: 0, clientX: 600, clientY: 300 });
    // The new box is selected: its inspector is open.
    expect(await screen.findByRole("heading", { name: "Device box" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]?.revision).toBe(7);
    expect(kinds(saved[0]?.document as MapDocument)).toEqual(["a", "b", "c1", "b1"]);
    // The editor closes after a save.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Done" })).toBeNull());
  });

  it("says to reload when someone else saved, and keeps the local copy", async () => {
    const { backend } = setup("admin", () => json({ detail: "stale revision" }, 409));
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Edit map" }));
    fireEvent.click(screen.getByRole("button", { name: "Legend" }));
    fireEvent.pointerDown(viewport(), { button: 0, clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Someone else saved this map. Reload to see their version.");
    expect(within(alert).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    // Still editing, with the legend still on the drawing.
    expect(viewport().querySelector('[data-kind="legend"]')).not.toBeNull();
  });

  it("shows the server's message on 422", async () => {
    const { backend } = setup("admin", () => json({ detail: "document too large" }, 422));
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Edit map" }));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("document too large");
  });

  it("removes a box with its cables, and undo brings them back", async () => {
    const { backend, saved } = setup("admin");
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Edit map" }));
    const alpha = viewport().querySelector('[data-element="a"]') as HTMLElement;
    fireEvent.pointerDown(alpha, { button: 0 });
    fireEvent.pointerUp(alpha);
    fireEvent.keyDown(document.body, { key: "Delete" });
    expect(viewport().querySelector('[data-element="a"]')).toBeNull();
    expect(viewport().querySelector('[data-cable="c1"]')).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(viewport().querySelector('[data-element="a"]')).not.toBeNull();
    expect(viewport().querySelector('[data-cable="c1"]')).not.toBeNull();

    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true, shiftKey: true });
    expect(viewport().querySelector('[data-element="a"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(kinds(saved[0]?.document as MapDocument)).toEqual(["b"]);
  });

  it("creates one box per device for 'start with my devices'", async () => {
    const created: unknown[] = [];
    const saves: { document: MapDocument; revision: number }[] = [];
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /maps": () => json([]),
        "POST /maps": (body) => {
          created.push(body);
          return json(
            { ...stored({ version: 1, vlans: [], elements: [] }), id: 2, revision: 1 },
            201,
          );
        },
        "PUT /maps/2": (body) => {
          saves.push(body as { document: MapDocument; revision: number });
          return json({ ...stored((body as { document: MapDocument }).document), id: 2 });
        },
        "GET /maps/2": () => json({ ...stored({ version: 1, vlans: [], elements: [] }), id: 2 }),
        "GET /instances": () =>
          json([
            instance("i1", "edge-fw", "Lab"),
            instance("i2", "core-sw"),
            instance("i3", "nas"),
          ]),
      },
    });
    renderApp("/map", backend);
    fireEvent.click(await screen.findByRole("button", { name: "New map" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Everything" } });
    fireEvent.click(screen.getByLabelText(/Start with my devices/));
    fireEvent.click(screen.getByRole("button", { name: "Create and start editing" }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(created).toEqual([{ name: "Everything" }]);
    const doc = saves[0]?.document as MapDocument;
    expect(doc.elements.map((e) => (e.kind === "box" ? e.instance_id : null)).sort()).toEqual([
      "i1",
      "i2",
      "i3",
    ]);
    expect(doc.elements.some((e) => e.kind === "cable")).toBe(false);
    expect(saves[0]?.revision).toBe(1);
  });
});

describe("editor model", () => {
  it("groups devices by site, sites first and no-site last", () => {
    const doc = devicesDocument(
      [instance("i1", "a"), instance("i2", "b", "Lab"), instance("i3", "c", "Lab")],
      () => undefined,
    );
    const at = (id: string) => doc.elements.find((e) => e.kind === "box" && e.instance_id === id);
    expect(at("i2")).toMatchObject({ x: 0, y: 0 });
    expect(at("i3")).toMatchObject({ y: 0 });
    expect((at("i1") as { y: number }).y).toBeGreaterThan(0);
  });

  it("removes cables with a removed element, and aligns to edges", () => {
    expect(removeElements(DOC, ["a"]).elements.map((e) => e.id)).toEqual(["b"]);
    const r = alignTo({ x: 103, y: 0, w: 50, h: 50 }, [{ x: 100, y: 200, w: 80, h: 20 }], 6);
    expect(r.dx).toBe(-3);
    expect(r.guides.v).toEqual([100]);
  });
});
