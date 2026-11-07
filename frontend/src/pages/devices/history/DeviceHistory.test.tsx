import { fireEvent, screen, waitFor } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../../../test-utils";
import { exampleInstance, INSTANCE_ID } from "../testFixtures";
import { chartQuery, historyChartsFor, rateRange } from "./historyCharts";

beforeEach(() => {
  mockSystemTheme(false);
});

const T0 = 1_800_000_000;
const matrix = (labels: Record<string, string>[], base = 10) => ({
  result_type: "matrix",
  warnings: [],
  series: labels.map((l, i) => ({
    labels: l,
    values: [0, 1, 2].map((n) => [T0 + n * 60, String(base + i + n)]),
  })),
});

interface QueryBody {
  query: string;
  start?: number;
  end?: number;
  step?: number;
}

function pageFor(typeId: string, answer: (body: QueryBody) => unknown) {
  const queries: QueryBody[] = [];
  const backend = fakeBackend({
    session: exampleMe("viewer"),
    routes: {
      "GET /instances": () => json([exampleInstance({ type_id: typeId })]),
      [`GET /instances/${INSTANCE_ID}`]: () => json(exampleInstance({ type_id: typeId })),
      [`GET /instances/${INSTANCE_ID}/cards`]: () => json([]),
      "POST /query": (body) => {
        queries.push(body as QueryBody);
        return json(answer(body as QueryBody));
      },
    },
  });
  renderApp(`/devices/${INSTANCE_ID}`, backend);
  return queries;
}

describe("device history", () => {
  it("draws ping latency and loss for the instance, and a range change re-asks", async () => {
    const queries = pageFor("ping_check", () => matrix([{}]));
    expect(await screen.findByRole("img", { name: /Round-trip time over the last 24 hours/ }));
    expect(screen.getByRole("img", { name: /Packet loss over the last 24 hours/ })).toBeVisible();
    const rtt = queries.find((q) => q.query.includes("rtt_avg_seconds"));
    expect(rtt?.query).toContain(`vantage_instance_id="${INSTANCE_ID}"`);
    expect((rtt?.end ?? 0) - (rtt?.start ?? 0)).toBe(86400);
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    expect(await screen.findByRole("img", { name: /Round-trip time over the last 7 days/ }));
    const week = queries.filter((q) => (q.end ?? 0) - (q.start ?? 0) === 7 * 86400);
    expect(week.length).toBeGreaterThan(0);
    // Within the proxy's budget of 1,100 points.
    for (const q of week) expect((7 * 86400) / (q.step ?? 1)).toBeLessThanOrEqual(1100);
  });

  it("lets a pfSense port be picked, busiest first, and shows in and out", async () => {
    const queries = pageFor("pfsense", (q) => {
      if (q.start === undefined && q.query.includes("sum by (interface)"))
        return {
          result_type: "vector",
          warnings: [],
          series: [
            { labels: { interface: "igc1" }, values: [[T0, "5"]] },
            { labels: { interface: "igc0" }, values: [[T0, "50"]] },
          ],
        };
      return matrix(
        q.query.includes("direction") ? [{ direction: "in" }, { direction: "out" }] : [{}],
      );
    });
    const select = await screen.findByRole("combobox", { name: "Traffic: port" });
    await waitFor(() => expect(select.querySelectorAll("option")).toHaveLength(3));
    expect([...select.querySelectorAll("option")].map((o) => o.textContent)).toEqual([
      "All ports",
      "igc0",
      "igc1",
    ]);
    fireEvent.change(select, { target: { value: "igc0" } });
    await waitFor(() =>
      expect(
        queries.some((q) => q.start !== undefined && q.query.includes('interface="igc0"')),
      ).toBe(true),
    );
    expect(
      await screen.findByRole("img", { name: /Traffic over the last 24 hours/ }),
    ).toBeVisible();
  });

  it("says there is no history yet when a chart's query returns nothing", async () => {
    pageFor("http_check", () => matrix([]));
    await waitFor(() => expect(screen.getAllByText("No history yet.")).toHaveLength(2));
  });

  it("has no History section for a type without charts", async () => {
    pageFor("fake_device", () => matrix([{}]));
    await screen.findByRole("heading", { level: 1, name: /nas-1/ });
    expect(screen.queryByRole("region", { name: "History" })).toBeNull();
  });
});

describe("history queries", () => {
  it("scope every chart to the instance and keep rate windows at least one step", () => {
    for (const type of [
      "pfsense",
      "snmp_device",
      "mikrotik_swos",
      "linux_host",
      "proxmox",
      "ping_check",
      "http_check",
      "emby",
      "smart_exporter",
      "cloudflared",
      "cloudflare",
    ]) {
      const charts = historyChartsFor(type);
      expect(charts.length).toBeGreaterThan(0);
      for (const chart of charts) {
        expect(chartQuery("abc", chart, 3600)).toContain('vantage_instance_id="abc"');
      }
    }
    for (const w of [3600, 86400, 30 * 86400]) {
      const seconds = Number.parseInt(rateRange(w), 10);
      expect(seconds).toBeGreaterThanOrEqual(300);
      expect(seconds).toBeGreaterThanOrEqual(Math.ceil(w / 360));
    }
    expect(historyChartsFor("unknown")).toEqual([]);
  });
});
