import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Instance, MapDocument, NetworkMap } from "../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";

/** A wide (desktop) or narrow (phone) screen; the map draws on one and lists on the other. */
function stubScreen(wide: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((media: string) => ({
      matches: media.includes("min-width") ? wide : false,
      media,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    })),
  );
}

beforeEach(() => {
  mockSystemTheme(false);
  stubScreen(true);
});

const XSS = "<img src=x onerror=alert(1)>";

function instance(id: string, name: string, type: string, state: "up" | "down"): Instance {
  return {
    id,
    type_id: type,
    name,
    site: null,
    tags: [],
    enabled: true,
    color: "violet",
    config: { host: "192.0.2.10" },
    secrets_set: {},
    status: { state, checked_at: null, last_success_at: null, error: null },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const box = (id: string, x: number, y: number, extra: Record<string, unknown> = {}) => ({
  id,
  kind: "box" as const,
  x,
  y,
  w: 200,
  h: 80,
  label: "",
  subtitle: "",
  icon: "server" as const,
  instance_id: null,
  show: { details: true, status: true, figures: true },
  figures: [],
  color: null,
  panel: null,
  ...extra,
});

// "core" is drawn last (lowest) and is DOWN; "edge" is up; "desk" is not monitored.
const DOC = {
  version: 1,
  vlans: [],
  elements: [
    box("edge", 40, 40, { instance_id: "i-edge" }),
    box("desk", 400, 40, { label: XSS }),
    box("core", 40, 300, { instance_id: "i-core" }),
    box("gone", 400, 300, { label: "Old NAS", instance_id: "i-gone" }),
    {
      id: "f1",
      kind: "frame",
      x: 0,
      y: 500,
      w: 400,
      h: 200,
      label: "Rack",
      instance_id: null,
      color: null,
      guests: { auto: true, hidden: [], order: [], manual: [] },
    },
    {
      id: "c1",
      kind: "cable",
      from: { element: "edge", port: null },
      to: { element: "core", port: null },
      speed: "1G",
      style: "solid",
      role: null,
      vlan: null,
      color: null,
      label: "uplink",
      show_rate: false,
      bends: [],
    },
    {
      id: "t1",
      kind: "text",
      x: 700,
      y: 40,
      w: 200,
      h: 80,
      text: "Line one\nLine two",
      size: "m",
      panel: true,
    },
    {
      id: "tb1",
      kind: "table",
      x: 700,
      y: 200,
      w: 240,
      h: 120,
      title: "Open ports",
      columns: ["Port", "Use"],
      rows: [{ cells: ["51820/udp", "WireGuard"], color: null }],
    },
  ],
} as unknown as MapDocument;

function mapOut(doc: MapDocument): NetworkMap {
  return { id: 1, name: "Home", position: 0, document: doc, revision: 1, updated_at: "2026-01-01" };
}

function backend(
  opts: {
    role?: "admin" | "viewer";
    maps?: unknown[];
    coreState?: "up" | "down";
    doc?: MapDocument;
  } = {},
) {
  return fakeBackend({
    session: exampleMe(opts.role ?? "viewer"),
    routes: {
      "GET /maps": () => json(opts.maps ?? [{ id: 1, name: "Home", position: 0 }]),
      "GET /maps/1": () => json(mapOut(opts.doc ?? DOC)),
      "GET /instances": () =>
        json([
          instance("i-edge", "edge-fw", "ping_check", "up"),
          instance("i-core", "core-sw", "ping_check", opts.coreState ?? "down"),
          instance("i-sw", "access-sw", "snmp_device", "up"),
        ]),
      // The switch reports port1 without link and port2 with link.
      "POST /query": (body) => {
        const q = (body as { query: string }).query;
        const port = (name: string, value: string, index: string) => ({
          labels: { vantage_instance_id: "i-sw", if_name: name, if_index: index },
          values: [[1, value]] as [number, string][],
        });
        const series = q.includes("if_oper_status")
          ? [port("port1", "2", "1"), port("port2", "1", "2")]
          : q.includes("if_admin_status")
            ? [port("port1", "1", "1"), port("port2", "1", "2")]
            : [];
        return json({ result_type: "vector", series, warnings: [] });
      },
      "POST /maps": () => json({ ...mapOut({ version: 1, vlans: [], elements: [] }), id: 2 }, 201),
      "GET /maps/2": () => json({ ...mapOut({ version: 1, vlans: [], elements: [] }), id: 2 }),
    },
  });
}

describe("map page", () => {
  it("draws boxes, frame, cable, text and table; a removed device and a label stay text", async () => {
    renderApp("/map", backend({ coreState: "up" }));
    const canvas = await screen.findByRole("region", { name: "Network map" });
    await within(canvas).findByText("edge-fw");
    expect(within(canvas).getByText("core-sw")).toBeInTheDocument();
    expect(within(canvas).getByText("Rack")).toBeInTheDocument();
    expect(within(canvas).getByText("Device removed")).toBeInTheDocument();
    expect(within(canvas).getByText("Not monitored")).toBeInTheDocument();
    expect(within(canvas).getByText("uplink")).toBeInTheDocument();
    expect(within(canvas).getByText("1G")).toBeInTheDocument();
    expect(within(canvas).getByText(/Line one/)).toBeInTheDocument();
    expect(within(canvas).getByText("Open ports")).toBeInTheDocument();
    expect(within(canvas).getByText("WireGuard")).toBeInTheDocument();
    // The map's tab and the New-map button for admins only.
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("button", { name: /new map/i })).not.toBeInTheDocument();

    // A label that looks like HTML is shown as text, never parsed.
    expect(within(canvas).getByText(XSS)).toBeInTheDocument();
    expect(canvas.querySelector("img")).toBeNull();
    expect(canvas.querySelector('[data-cable="c1"]')).not.toHaveAttribute("data-down");
  });

  it("turns a DOWN device's box and its cable red", async () => {
    renderApp("/map", backend());
    const canvas = await screen.findByRole("region", { name: "Network map" });
    await waitFor(() =>
      expect(canvas.querySelector('[data-element="core"]')).toHaveAttribute("data-down", "true"),
    );
    expect(canvas.querySelector('[data-element="edge"]')).not.toHaveAttribute("data-down");
    expect(canvas.querySelector('[data-cable="c1"]')).toHaveAttribute("data-down", "true");
    expect(within(canvas).getByText("DOWN")).toBeInTheDocument();
  });

  it("lists problems first on a phone", async () => {
    stubScreen(false);
    renderApp("/map", backend());
    const list = await screen.findByRole("region", { name: "Map devices" });
    await waitFor(() => expect(within(list).getByText("DOWN")).toBeInTheDocument());
    const order = Array.from(list.querySelectorAll("[data-row]")).map((r) =>
      r.getAttribute("data-row"),
    );
    // core is drawn below edge but is the problem: first. The rest in drawing order.
    expect(order).toEqual(["core", "edge", "desk", "gone"]);
    expect(within(list).getByText(/via edge-fw · 1G/)).toBeInTheDocument();
    fireEvent.click(within(list).getByText("Open ports"));
    // A phone gets the list only, not the drawing.
    expect(screen.queryByRole("region", { name: "Network map" })).not.toBeInTheDocument();
  });

  it("shows the empty state, with New map for admins only", async () => {
    const admin = backend({ role: "admin", maps: [] });
    const { unmount } = renderApp("/map", admin);
    expect(await screen.findByText("No maps yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New map" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Home" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and start editing" }));
    await waitFor(() => expect(admin.requests().map((r) => r.route)).toContain("POST /maps"));
    unmount();

    renderApp("/map", backend({ role: "viewer", maps: [] }));
    expect(await screen.findByText("No maps yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New map" })).not.toBeInTheDocument();
    expect(screen.getByText(/An admin can draw/)).toBeInTheDocument();
  });
});

const cable = (id: string, from: [string, string | null], to: [string, string | null]) => ({
  id,
  kind: "cable" as const,
  from: { element: from[0], port: from[1] },
  to: { element: to[0], port: to[1] },
  speed: "1G" as const,
  style: "solid" as const,
  role: null,
  vlan: null,
  color: null,
  label: "",
  show_rate: false,
  bends: [],
});

const PANEL = {
  look: "flat",
  place: "bottom",
  size: "m",
  numbers: true,
  captions: "typed",
  source: "manual",
};
const GROUP = (id: string, kind: string) => ({
  id,
  kind,
  count: 1,
  rows: 1,
  order: "rows",
  first: 1,
  prefix: "",
});
const PORT = (id: string, group: string, vlans: unknown) => ({
  id,
  group,
  n: 1,
  caption: "",
  interface: "",
  vlans,
  hidden: false,
});

// "sw" shows the switch's own ports; "man" has two typed ports with VLANs.
const PORT_DOC = {
  version: 1,
  vlans: [
    { id: "10", name: "Users", color: "teal" },
    { id: "20", name: "Voice", color: "pink" },
  ],
  elements: [
    box("sw", 40, 40, {
      instance_id: "i-sw",
      h: 120,
      // Read from the device, with no ports of its own yet: the interfaces are listed live.
      panel: { ...PANEL, source: "device", groups: [], ports: [] },
    }),
    box("edge", 40, 400, { instance_id: "i-edge" }),
    box("man", 400, 40, {
      label: "Patch panel",
      h: 120,
      panel: {
        ...PANEL,
        groups: [GROUP("g1", "rj45"), GROUP("g2", "sfp_plus")],
        ports: [
          PORT("a", "g1", { untagged: "10", tagged: ["20"] }),
          PORT("b", "g2", { untagged: null, tagged: [] }),
        ],
      },
    }),
    cable("down", ["sw", "port1"], ["edge", null]),
    cable("fine", ["sw", "port2"], ["man", "a"]),
  ],
} as unknown as MapDocument;

describe("map page, live view", () => {
  it("draws a typed port strip with jacks, cages and VLAN bars", async () => {
    renderApp("/map", backend({ doc: PORT_DOC }));
    const canvas = await screen.findByRole("region", { name: "Network map" });
    const man = canvas.querySelector('[data-element="man"]') as HTMLElement;
    expect(man.querySelectorAll("[data-port]")).toHaveLength(2);
    expect(man.querySelector('[data-port="a"] [data-vlan="10"]')).not.toBeNull(); // untagged
    expect(man.querySelector('[data-port="a"] [data-vlan="20"]')).not.toBeNull(); // tagged
    // An RJ45 jack and an SFP+ cage, each with its number above.
    expect(man.querySelector('[data-port="a"]')).toHaveAttribute("data-jack", "rj45");
    expect(man.querySelector('[data-port="b"]')).toHaveAttribute("data-jack", "cage");
    expect(man.querySelector('[data-port="a"] [data-number]')?.textContent).toBe("1");
  });

  it("turns the cable of a port without link red, and leaves the others", async () => {
    renderApp("/map", backend({ doc: PORT_DOC }));
    const canvas = await screen.findByRole("region", { name: "Network map" });
    await waitFor(() =>
      expect(canvas.querySelector('[data-port="port1"]')).toHaveAttribute("data-state", "down"),
    );
    expect(canvas.querySelector('[data-port="port2"]')).toHaveAttribute("data-state", "up");
    expect(canvas.querySelector('[data-cable="down"]')).toHaveAttribute("data-down", "true");
    expect(canvas.querySelector('[data-cable="fine"]')).not.toHaveAttribute("data-down");
  });

  it("opens the details drawer on a click, links to the device and closes on Escape", async () => {
    renderApp("/map", backend());
    const canvas = await screen.findByRole("region", { name: "Network map" });
    const trigger = await within(canvas).findByRole("button", { name: "Details of edge-fw" });
    trigger.focus();
    fireEvent.click(trigger);
    const drawer = await within(canvas).findByRole("complementary", { name: "Details of edge-fw" });
    // Focus moves into the drawer, and back to the box when it closes.
    expect(drawer).toHaveFocus();
    expect(within(drawer).getByRole("link", { name: "Open device page" })).toHaveAttribute(
      "href",
      "/devices/i-edge",
    );
    expect(within(drawer).getByText(/core-sw/)).toBeInTheDocument(); // the cable's other end
    fireEvent.keyDown(window, { key: "Escape" });
    expect(within(canvas).queryByRole("complementary")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("shows the cable card when a cable gets keyboard focus", async () => {
    renderApp("/map", backend());
    const canvas = await screen.findByRole("region", { name: "Network map" });
    const cable = await within(canvas).findByRole("button", { name: /^Cable .* to /i });
    fireEvent.focus(cable);
    expect(await within(canvas).findByRole("tooltip")).toBeInTheDocument();
    fireEvent.blur(cable);
    expect(within(canvas).queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
