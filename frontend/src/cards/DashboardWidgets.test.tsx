import { screen, within } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { exampleInstance, INSTANCE_ID } from "../pages/devices/testFixtures";
import type { AlertEvent, Instance } from "../api";
import { hasWanSource, healthCounts, wanSummary, withoutFreshData } from "./overview";
import { ageText, clockTime, findProblems, problemSubline, sinceText } from "./problems";

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

  it("counts devices without fresh data", () => {
    const now = Date.parse("2026-01-02T03:10:00Z");
    const status = (state: Instance["status"]["state"], last: string | null) => ({
      state,
      checked_at: null,
      last_success_at: last,
      error: null,
    });
    const fresh = exampleInstance({ status: status("up", "2026-01-02T03:09:00Z") });
    const old = exampleInstance({ status: status("down", "2026-01-02T03:00:00Z") });
    const pending = exampleInstance({ status: status("pending", null) });
    const scraped = exampleInstance({ id: "scraped", status: status("unknown", null) });
    const disabled = exampleInstance({ enabled: false, status: status("unknown", null) });
    const scrape = new Map([["scraped", "down" as const]]);
    expect(withoutFreshData([fresh, old, pending, scraped, disabled], scrape, now)).toBe(2);
  });
});

describe("problems", () => {
  const event = (overrides: Partial<AlertEvent>): AlertEvent => ({
    id: 1,
    rule_id: 1,
    instance_id: null,
    rule_name: "High CPU",
    severity: "warning",
    labels: {},
    fingerprint: "example",
    state: "firing",
    no_data_reason: null,
    started_at: "2026-01-02T03:00:00Z",
    state_since: null,
    ended_at: null,
    end_reason: null,
    summary: "x",
    deliveries: [],
    ...overrides,
  });

  it("folds a down device's alerts into its row and sorts oldest first", () => {
    const now = Date.parse("2026-01-02T03:30:00Z");
    const down = exampleInstance({
      name: "router-1",
      status: {
        state: "down",
        checked_at: null,
        last_success_at: "2026-01-02T03:10:00Z",
        error: "no answer to SNMP",
      },
    });
    const other = exampleInstance({ id: "other", name: "nas-2" });
    const events = [
      event({ id: 1, instance_id: INSTANCE_ID, rule_name: "Router down" }),
      event({
        id: 2,
        instance_id: "other",
        rule_name: "Disk full",
        state: "no_data",
        no_data_reason: "source_stale",
        state_since: "2026-01-02T03:20:00Z",
      }),
      event({ id: 3, rule_name: "Ping loss", started_at: "2026-01-02T03:05:00Z" }),
    ];
    const problems = findProblems([down, other], events, (i) => i.status.state, now);
    expect(problems.map((p) => [p.tag, p.title, p.detail, p.href])).toEqual([
      ["FIRING", "Ping loss", "x", "/alerts"],
      ["DOWN", "router-1 is down", "no answer to SNMP · 1 open alert", "/devices"],
      ["NO DATA", "Disk full", "device not reporting · nas-2", "/alerts"],
    ]);
    expect(sinceText(problems[1]?.since ?? null, now)).toMatch(/^since \d\d:\d\d · 20 min$/);
    const started = clockTime(Date.parse("2026-01-02T03:05:00Z"), now);
    expect(problemSubline(problems)).toBe(
      `Ping loss firing since ${started}. 1 other device down.`,
    );
    expect(ageText(125 * 60_000)).toBe("2 h 5 min");
  });
});

describe("dashboard headline and summary tiles", () => {
  it("shows the problems strip and the tiles, with WAN from a fleet query", async () => {
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
              instance_id: null,
              rule_name: "High CPU",
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
    expect(await screen.findByRole("heading", { level: 2, name: "1 problem" })).toBeInTheDocument();
    const strip = screen.getByRole("region", { name: "Active problems" });
    expect(within(strip).getByText("FIRING")).toBeInTheDocument();
    expect(within(strip).getByText("High CPU")).toBeInTheDocument();
    expect(within(strip).getByRole("link", { name: "Open High CPU" })).toHaveAttribute(
      "href",
      "/alerts",
    );

    const internet = screen.getByRole("group", { name: "Internet" });
    expect(await within(internet).findByText("1/2")).toBeInTheDocument();
    // The down gateway is the one named.
    expect(within(internet).getByText("192.0.2.2 · 100% loss")).toBeInTheDocument();
    const alerts = screen.getByRole("group", { name: "Active alerts" });
    expect(within(alerts).getByText("1")).toBeInTheDocument();
    expect(within(alerts).getByText("firing")).toBeInTheDocument();
    const devices = screen.getByRole("group", { name: "Devices" });
    expect(within(devices).getByText("1/1")).toBeInTheDocument();
    // The example device is up but has no success on record.
    expect(within(devices).getByText("1 device without fresh data")).toBeInTheDocument();
    expect(screen.getByText("No port chosen.")).toBeInTheDocument();
  });

  it("says all systems normal when nothing is wrong", async () => {
    const checked = new Date(Date.now() - 5_000).toISOString();
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: {
        "GET /instances": () =>
          json([
            exampleInstance({
              status: { state: "up", checked_at: checked, last_success_at: checked, error: null },
            }),
          ]),
      },
    });
    renderApp("/", backend);
    expect(
      await screen.findByRole("heading", { level: 2, name: "All systems normal" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/^1 device up · no active alerts · checked \d+ s ago$/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Active problems" })).not.toBeInTheDocument();
  });
});
