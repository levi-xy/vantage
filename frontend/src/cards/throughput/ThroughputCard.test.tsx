import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance, QueryRequest, QueryResult } from "../../api";
import { json } from "../../test-utils";
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

function renderCard(instance: Instance, cardSettings: Record<string, string> = settings) {
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
  render(
    <QueryClientProvider client={queryClient}>
      <ThroughputCard instances={[instance]} settings={cardSettings} />
    </QueryClientProvider>,
  );
  return queries;
}

describe("ThroughputCard", () => {
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

  it("says in and out for a port that isn't the WAN", async () => {
    renderCard(firewall(10, false));
    const inbound = await screen.findByRole("group", { name: "In" });
    expect(await within(inbound).findByText("84.2 Mbit/s")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Out" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Download" })).not.toBeInTheDocument();
  });
});
