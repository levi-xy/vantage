import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance, QueryResult } from "../../api";
import { json } from "../../test-utils";
import { PingCheckCard } from "./PingCheckCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

const instance = {
  id: "inst-1",
  name: "router-ping",
  status: { state: "up", checked_at: null, last_success_at: null, error: null },
} as Instance;

function vector(value: string | null): QueryResult {
  return {
    result_type: "vector",
    series: value === null ? [] : [{ labels: {}, values: [[1_700_000_000, value]] }],
    warnings: [],
  };
}

function renderCard(answer: (query: string, range: boolean) => QueryResult | Response) {
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
    const { query, start } = JSON.parse(String(init?.body)) as { query: string; start?: number };
    const out = answer(query, start !== undefined);
    return out instanceof Response ? out : json(out);
  });
  vi.spyOn(client, "query").mockImplementation((req) =>
    new ApiClient({ fetch: fetchMock }).query(req),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <PingCheckCard instanceId="inst-1" settings={{}} title="Ping check" instance={instance} />
    </QueryClientProvider>,
  );
}

describe("PingCheckCard", () => {
  it("shows round-trip time and packet loss", async () => {
    renderCard((q) => vector(q.startsWith("vantage_ping_check_rtt") ? "0.0123" : "0"));
    expect(await screen.findByText("12.3 ms")).toBeInTheDocument();
    // Healthy is plain text: no green, and the header says a muted UP.
    expect(screen.getByText("0.0%").className).not.toMatch(/emerald|crit/);
    expect(screen.getByText("router-ping")).toBeInTheDocument();
    expect(screen.getByText("UP").className).toContain("text-ok-text");
  });

  it("shows 100% loss as the header's state, in the critical colour", async () => {
    renderCard((q) => vector(q.startsWith("vantage_ping_check_rtt") ? null : "1"));
    expect(await screen.findByText("100% LOSS")).toHaveClass("text-crit-text");
    expect(screen.getByText("100%")).toHaveClass("text-crit-text");
  });

  it("shows partial loss in the warn colour, and the hour's sparkline with min, avg and max", async () => {
    const hour: QueryResult = {
      result_type: "matrix",
      series: [
        {
          labels: {},
          values: [
            [1_700_000_000, "0.001"],
            [1_700_000_060, "0.002"],
            [1_700_000_120, "0.006"],
          ],
        },
      ],
      warnings: [],
    };
    renderCard((q, range) =>
      range ? hour : vector(q.startsWith("vantage_ping_check_rtt") ? "0.002" : "0.25"),
    );
    expect(await screen.findByText("25%")).toHaveClass("text-warn");
    expect(
      await screen.findByRole("img", { name: "Round trip time, last hour" }),
    ).toBeInTheDocument();
    expect(screen.getByText("min 1.0 ms · avg 3.0 ms · max 6.0 ms")).toBeInTheDocument();
  });

  it("says so when there is no data", async () => {
    renderCard(() => vector(null));
    expect(await screen.findByText(/No data yet/)).toBeInTheDocument();
  });

  it("shows an error when the query fails", async () => {
    renderCard(() => json({ detail: "query too expensive" }, 422));
    expect(await screen.findByRole("alert")).toHaveTextContent("query too expensive");
  });
});
