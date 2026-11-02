import { screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance } from "../../api";
import { PingCheckCard } from "../ping-check/PingCheckCard";
import { renderCard, testInstance, vector } from "../testRender";
import { formatAsOf } from "./freshness";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

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
        // Live and sparkline queries are empty. Only the moment the last poll's samples were
        // in Prometheus (one scrape interval after it) has values.
        if (req.start !== last + 30 || req.end !== last + 30) return vector([]);
        return vector([[{}, query.includes("rtt") ? "0.0123" : "0"]]);
      },
      instance,
      { cached: true },
    );

    expect(
      await screen.findByText(`Values as of ${formatAsOf(last)}, greyed.`),
    ).toBeInTheDocument();
    // The header: a short state with the full error as its tooltip, never the error text.
    const state = screen.getByText("DOWN 10m");
    expect(state.className).toContain("text-crit-text");
    expect(state).toHaveAttribute("title", "timed out");
    expect(screen.queryByText("timed out")).not.toBeInTheDocument();
    expect(screen.getByText(`Device · no data since ${formatAsOf(last)}`)).toBeInTheDocument();
    const loss = screen.getByText("0.0%");
    expect(loss.closest('[data-stale="true"]')).not.toBeNull();
    // Zero loss would be green if it were current; a past value stays neutral.
    expect(loss.className).not.toMatch(/emerald|crit/);
    expect(screen.getByText("12.3 ms")).toBeInTheDocument();
  });
});
