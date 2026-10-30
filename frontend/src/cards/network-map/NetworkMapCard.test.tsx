import { fireEvent, screen } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../../test-utils";
import { exampleInstance, INSTANCE_ID } from "../../pages/devices/testFixtures";
import { vector } from "../testRender";
import { circleLayout, drawableLinks, findPort } from "./mapLayout";

beforeEach(() => mockSystemTheme(false));

const OTHER_ID = "00000000-0000-4000-8000-000000000002";
const nas = exampleInstance();
const fw = exampleInstance({ id: OTHER_ID, name: "fw-1" });
const link = {
  id: 7,
  a_instance_id: INSTANCE_ID,
  b_instance_id: OTHER_ID,
  label: "uplink",
  a_port: "port1",
  b_port: "igc0",
};

describe("network map helpers", () => {
  it("places every instance, and only draws links between known ones", () => {
    expect(circleLayout([nas, fw]).size).toBe(2);
    expect(drawableLinks([link], [nas, fw])).toEqual([link]);
    expect(drawableLinks([link], [nas])).toEqual([]);
  });

  it("finds a port by interface name or shown name, per instance", () => {
    const groups = [
      {
        instance: nas,
        ports: [
          {
            port: { source: "snmp" as const, instanceId: INSTANCE_ID, iface: "port1" },
            name: "Uplink",
          },
        ],
      },
    ];
    expect(findPort(groups, INSTANCE_ID, "PORT1")?.iface).toBe("port1");
    expect(findPort(groups, INSTANCE_ID, "uplink")?.iface).toBe("port1");
    expect(findPort(groups, OTHER_ID, "port1")).toBeNull();
    expect(findPort(groups, INSTANCE_ID, null)).toBeNull();
  });
});

describe("network map card", () => {
  const layout = {
    version: 2,
    cards: [{ instance_id: null, card_type: "network-map", x: 0, y: 0, w: 6, h: 4, settings: {} }],
  };

  it("draws devices and a labelled link, with traffic when both ports have counters", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /instances": () => json([nas, fw]),
        "GET /links": () => json([link]),
        "GET /dashboards/1": () => json({ id: 1, name: "Default", layout }),
        "POST /query": (body) => {
          const q = String((body as { query: string }).query);
          if (q.includes("if_in_bytes_total") && q.startsWith("count"))
            return json(vector([[{ vantage_instance_id: INSTANCE_ID, if_name: "port1" }, "1"]]));
          if (q.startsWith("count by") && q.includes("net_bytes_recv"))
            return json(vector([[{ vantage_instance_id: OTHER_ID, interface: "igc0" }, "1"]]));
          return json(vector([[{}, "125000"]]));
        },
      },
    });
    renderApp("/", backend);
    expect(await screen.findByText("nas-1")).toBeInTheDocument();
    expect(screen.getByText("fw-1")).toBeInTheDocument();
    expect(await screen.findByText(/uplink.*↓.*↑/)).toBeInTheDocument();
  });

  it("lets an admin add a link, sending the CSRF token", async () => {
    const created: unknown[] = [];
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /instances": () => json([nas, fw]),
        "GET /links": () => json([]),
        "POST /links": (body) => {
          created.push(body);
          return json({ id: 1, ...(body as object) }, 201);
        },
        "GET /dashboards/1": () => json({ id: 1, name: "Default", layout }),
      },
    });
    renderApp("/", backend);
    fireEvent.click(await screen.findByRole("button", { name: /edit dashboard/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Settings of Network map" }));
    fireEvent.change(await screen.findByLabelText("Device A"), { target: { value: INSTANCE_ID } });
    fireEvent.change(screen.getByLabelText("Device B"), { target: { value: OTHER_ID } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "uplink" } });
    fireEvent.click(screen.getByRole("button", { name: "Add link" }));
    await vi.waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toMatchObject({ a_instance_id: INSTANCE_ID, b_instance_id: OTHER_ID });
    expect(backend.requests().find((r) => r.route === "POST /links")?.csrf).toBeTruthy();
  });
});
