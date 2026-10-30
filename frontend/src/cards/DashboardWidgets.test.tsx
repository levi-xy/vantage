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

describe("dashboard-wide cards", () => {
  it("shows on an old layout, with WAN from a fleet query", async () => {
    const backend = fakeBackend({
      session: exampleMe("admin"),
      routes: {
        "GET /instances": () => json([exampleInstance({ type_id: "pfsense" })]),
        [`GET /instances/${INSTANCE_ID}/cards`]: () => json([]),
        "GET /dashboards/1": () => json({ id: 1, name: "Default", layout: { cards: [] } }),
        "GET /alerts/events": () =>
          json([
            {
              id: 2,
              rule_id: 1,
              state: "firing",
              started_at: "2026-01-01T00:00:00Z",
              ended_at: null,
              summary: "x",
            },
          ]),
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
    expect(screen.getByText("No port chosen.")).toBeInTheDocument();
    expect(await screen.findByText("Active alerts")).toBeInTheDocument();
    expect(await screen.findByText("1")).toBeInTheDocument();
    expect(screen.getByText("Devices healthy")).toBeInTheDocument();
  });
});
