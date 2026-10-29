import { screen } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { exampleInstance, INSTANCE_ID } from "../pages/devices/testFixtures";
import { hasWanSource, healthCounts, wanSummary } from "./overview";

beforeEach(() => mockSystemTheme(false));

const result = (values: string[]) => ({
  result_type: "vector" as const,
  series: values.map((v, i) => ({
    labels: { url: `192.0.2.${i + 1}` },
    values: [[1, v] as [number, string]],
  })),
  warnings: [],
});

describe("overview helpers", () => {
  it("counts states and treats 100 % loss as a down gateway", () => {
    const up = exampleInstance();
    const down = exampleInstance({ status: { ...up.status, state: "down" } });
    expect(healthCounts([up, down, down])).toMatchObject({ down: 2 });
    expect(wanSummary(result(["0", "100", "20"]))).toEqual({ up: 2, total: 3 });
    expect(hasWanSource([up])).toBe(false);
    expect(hasWanSource([exampleInstance({ type_id: "pfsense" })])).toBe(true);
  });
});

describe("overview row", () => {
  it("shows the tiles, with WAN and throughput from fleet queries", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /instances": () => json([exampleInstance({ type_id: "pfsense" })]),
        [`GET /instances/${INSTANCE_ID}/cards`]: () => json([]),
        "GET /dashboards/1": () => json({ id: 1, name: "Default", layout: { cards: [] } }),
        "POST /query": (body) =>
          json(
            String((body as { query: string }).query).includes("packet_loss")
              ? result(["0", "100"])
              : result(["2500000"]),
          ),
      },
    });
    renderApp("/", backend);
    expect(await screen.findByText("1 of 2 gateways up")).toBeInTheDocument();
    expect(await screen.findByText("2.5 Mbit/s")).toBeInTheDocument();
    expect(screen.getByText("Alerts not set up yet")).toBeInTheDocument();
    expect(screen.getByText("Devices healthy")).toBeInTheDocument();
  });
});
