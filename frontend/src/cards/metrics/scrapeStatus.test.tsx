import { screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance } from "../../api";
import { PlaceholderCard } from "../PlaceholderCard";
import { renderCard, testInstance, vector } from "../testRender";
import { scrapeStates, shownState } from "./scrapeStatus";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

const scrapeOnly = (state: Instance["status"]["state"], enabled = true): Instance => ({
  ...testInstance,
  enabled,
  status: { ...testInstance.status, state },
});

describe("scrape status", () => {
  it("uses Prometheus' up only for enabled instances the collector doesn't poll", () => {
    const scrape = scrapeStates(
      vector([
        [{ vantage_instance_id: "inst-1" }, "1"],
        [{ vantage_instance_id: "other" }, "0"],
      ]),
    );
    expect(scrape.get("other")).toBe("down");
    expect(shownState(scrapeOnly("unknown"), scrape)).toBe("up");
    expect(shownState(scrapeOnly("down"), scrape)).toBe("down"); // polled: backend wins
    expect(shownState(scrapeOnly("unknown", false), scrape)).toBe("unknown"); // disabled
    expect(shownState(scrapeOnly("unknown"), new Map())).toBe("unknown"); // no target yet
  });

  it("a scrape-only card shows Up when Prometheus scrapes it", async () => {
    renderCard(
      client,
      PlaceholderCard,
      (query) =>
        query.startsWith("max by (vantage_instance_id) (up")
          ? vector([[{ vantage_instance_id: "inst-1" }, "1"]])
          : vector([]),
      scrapeOnly("unknown"),
    );
    expect(await screen.findByText("Up")).toBeInTheDocument();
  });
});
