import { screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance } from "../../api";
import { PingCheckCard } from "../ping-check/PingCheckCard";
import { renderCard, testInstance, vector } from "../testRender";
import { formatAsOf } from "./freshness";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));
// ECharts needs a real canvas; the chart itself is not rendered under jsdom.
vi.mock("./TimeSeriesChart", () => ({ TimeSeriesChart: () => null }));

describe("a card with a stale value", () => {
  it("shows the past value greyed, with neutral colours and 'as of'", async () => {
    const last = Math.floor(Date.now() / 1000) - 600;
    const instance: Instance = {
      ...testInstance,
      enabled: true,
      status: {
        state: "down",
        checked_at: null,
        last_success_at: new Date(last * 1000).toISOString(),
        error: "timed out",
      },
    };
    renderCard(
      client,
      PingCheckCard,
      (query, req) => {
        // Live and sparkline queries are empty; only the past moment has values.
        if (req.start === undefined || req.start !== req.end) return vector([]);
        return vector([[{}, query.includes("rtt") ? "0.0123" : "0"]]);
      },
      instance,
      { cached: true },
    );

    expect(await screen.findByText(`as of ${formatAsOf(last)}`)).toBeInTheDocument();
    const loss = screen.getByText("0.0%");
    expect(loss.closest('[data-stale="true"]')).not.toBeNull();
    // Zero loss would be green if it were current; a past value stays neutral.
    expect(loss.className).not.toMatch(/emerald|red/);
    expect(screen.getByText("12.3 ms")).toBeInTheDocument();
  });
});
