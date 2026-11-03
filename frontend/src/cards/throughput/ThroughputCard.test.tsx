import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance, QueryRequest, QueryResult } from "../../api";
import { LIVE_RATE_KEY } from "../../app/live";
import type { LiveRate } from "../../app/live";
import { json } from "../../test-utils";
import { MainDashboardContext } from "../mainDashboard";
import { CardTierContext } from "../metrics/tier";
import type { CardTier } from "../metrics/tier";
import { ThroughputCard } from "./ThroughputCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

const settings = { port1: "pfsense:inst-1:igc0" };

/** A pfSense whose igc0 is mapped to "WAN" in its settings, unless `wan` is false. */
function firewall(lastSuccessSecondsAgo: number, wan = true): Instance {
  return {
    id: "inst-1",
    name: "fw-1",
    type_id: "pfsense",
    enabled: true,
    config: { interface_names: wan ? ["igc0=WAN"] : [] },
    status: {
      state: "up",
      checked_at: null,
      last_success_at: new Date(Date.now() - lastSuccessSecondsAgo * 1000).toISOString(),
      error: null,
    },
  } as unknown as Instance;
}

const result = (type: "vector" | "matrix", values: [number, string][]): QueryResult => ({
  result_type: type,
  series: values.length ? [{ labels: {}, values }] : [],
  warnings: [],
});

function renderCard(
  instance: Instance,
  cardSettings: Record<string, string> = settings,
  live?: LiveRate,
  tier: CardTier = "full",
  onMain = true,
) {
  const queries: QueryRequest[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
    const req = JSON.parse(String(init?.body)) as QueryRequest;
    queries.push(req);
    const download = req.query.includes("net_bytes_recv{");
    if (req.start !== undefined) {
      const t0 = req.start;
      return json(
        result("matrix", [
          [t0, download ? "40000000" : "5000000"],
          [t0 + 3600, download ? "84200000" : "12600000"],
        ]),
      );
    }
    if (req.query.startsWith("count by (vantage_instance_id, interface)")) {
      const labels = { vantage_instance_id: "inst-1", interface: "igc0" };
      return json({
        result_type: "vector",
        series: [{ labels, values: [[0, "1"]] }],
        warnings: [],
      });
    }
    if (req.query.startsWith("8 *")) {
      return json(result("vector", [[0, download ? "84200000" : "12600000"]]));
    }
    return json(result("vector", []));
  });
  vi.spyOn(client, "query").mockImplementation((req) =>
    new ApiClient({ fetch: fetchMock }).query(req),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (live) queryClient.setQueryData(LIVE_RATE_KEY, live);
  render(
    <QueryClientProvider client={queryClient}>
      <MainDashboardContext.Provider value={onMain}>
        <CardTierContext.Provider value={{ tier }}>
          <ThroughputCard instances={[instance]} settings={cardSettings} />
        </CardTierContext.Provider>
      </MainDashboardContext.Provider>
    </QueryClientProvider>,
  );
  return queries;
}

describe("ThroughputCard", () => {
  it("ignores the pushed live rate on any dashboard but the main one", async () => {
    const live = { inBps: 1_000_000, outBps: 1_000_000, ports: 1, receivedAt: Date.now() };
    renderCard(firewall(10), settings, live, "full", false);
    const download = await screen.findByRole("group", { name: "Download" });
    expect(await within(download).findByText("84.2 Mbit/s")).toBeInTheDocument();
    expect(screen.queryByText(/· live/)).not.toBeInTheDocument();
  });

  it("asks to choose a port first", () => {
    renderCard(firewall(10), {});
    expect(screen.getByText("No port chosen.")).toBeInTheDocument();
  });

  it("shows download and upload now, and a 6-hour chart", async () => {
    const queries = renderCard(firewall(10));
    const download = await screen.findByRole("group", { name: "Download" });
    expect(await within(download).findByText("84.2 Mbit/s")).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "Upload" })).getByText("12.6 Mbit/s"),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("img", { name: "Download and upload over the last 6 hours" }),
    ).toBeInTheDocument();
    // Gridlines in the chosen unit, from 0 to a round top above 84.2 Mbit/s.
    expect(screen.getAllByText("100 Mbit/s").length).toBeGreaterThan(0);
    const range = queries.find((q) => q.start !== undefined);
    expect(range && (range.end ?? 0) - (range.start ?? 0)).toBe(6 * 3600);
  });

  it("greys the history and shows no current value when the device has no fresh data", async () => {
    renderCard(firewall(600));
    const chart = await screen.findByRole("img", {
      name: "Download and upload over the last 6 hours",
    });
    expect(chart.closest('[data-stale="true"]')).not.toBeNull();
    expect(screen.getByText("No recent data from fw-1; history greyed.")).toBeInTheDocument();
    expect(
      within(await screen.findByRole("group", { name: "Download" })).getByText("—"),
    ).toBeTruthy();
  });

  it("shows a pushed live rate in the same unit as the chart (bits per second, as sent)", async () => {
    // The chart's last point is 84.2 Mbit/s (8 * rate of the byte counter, in Prometheus);
    // /ws pushes bits per second already, so 84 000 000 must read 84.0 Mbit/s, not x8.
    renderCard(firewall(10), settings, {
      inBps: 84_000_000,
      outBps: 12_000_000,
      ports: 1,
      receivedAt: Date.now(),
    });
    const download = await screen.findByRole("group", { name: "Download" });
    expect(within(download).getByText("84.0 Mbit/s")).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "Upload" })).getByText("12.0 Mbit/s"),
    ).toBeInTheDocument();
    expect(await screen.findByText(/· live/)).toBeInTheDocument();
    // The chart's top gridline is the round step above its 84.2 Mbit/s last point.
    expect(await screen.findAllByText("100 Mbit/s")).not.toHaveLength(0);
  });

  it("small: the current rates, no chart; NO DATA when the device doesn't answer", async () => {
    renderCard(firewall(10), settings, undefined, "small");
    const rates = await screen.findByRole("group", { name: "Download and upload" });
    await vi.waitFor(() => expect(rates).toHaveTextContent("↓84.2 Mbit/s ↑12.6 Mbit/s"));
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("NO DATA")).not.toBeInTheDocument();
  });

  it("small, stale: NO DATA in red and no current value", async () => {
    renderCard(firewall(600), settings, undefined, "small");
    expect((await screen.findByText("NO DATA")).className).toContain("text-crit-text");
    const rates = await screen.findByRole("group", { name: "Download and upload" });
    expect(rates).toHaveTextContent("↓— ↑—");
  });

  it("says in and out for a port that isn't the WAN", async () => {
    renderCard(firewall(10, false));
    const inbound = await screen.findByRole("group", { name: "In" });
    expect(await within(inbound).findByText("84.2 Mbit/s")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Out" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Download" })).not.toBeInTheDocument();
  });
});
