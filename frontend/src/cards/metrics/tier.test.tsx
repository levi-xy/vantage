import { screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import type { Instance } from "../../api";
import { LinuxHostCard } from "../linux-host/LinuxHostCard";
import { PingCheckCard } from "../ping-check/PingCheckCard";
import { SmartDisksCard } from "../smart-disks/SmartDisksCard";
import { renderCard, testInstance, vector } from "../testRender";
import { cardFullSize, cardMinSize, cardTier, WIDGETS } from "../widgets";
import { tierFor } from "./tier";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({
  useApiClient: () => client,
  useCanEditDashboards: () => true,
}));

describe("card tiers", () => {
  it("are full from the full size, medium from 3 x 2, small below", () => {
    expect(tierFor([4, 3], [4, 3])).toBe("full");
    expect(tierFor([12, 8], [4, 3])).toBe("full");
    expect(tierFor([3, 2], [4, 3])).toBe("medium");
    expect(tierFor([12, 2], [4, 3])).toBe("medium");
    expect(tierFor([12, 1], [4, 3])).toBe("small");
    expect(tierFor([2, 8], [4, 3])).toBe("small");
    // Every card can be made 2 x 1; it is added at its full size.
    expect(cardMinSize()).toEqual([2, 1]);
    expect(cardTier("switch-panel", "inst-1", [6, 5])).toBe("full");
    expect(cardTier("switch-panel", "inst-1", [2, 1])).toBe("small");
    expect(cardFullSize("devices-health", null)).toEqual([2, 2]);
    for (const w of WIDGETS) expect(cardTier(w.card_type, null, w.default_size)).toBe("full");
  });

  it("small: a down device shows DOWN and its figure greyed, never as if fine", async () => {
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
        if (req.start !== last + 30 || req.end !== last + 30) return vector([]);
        return vector([[{}, query.includes("rtt") ? "0.0123" : "0"]]);
      },
      instance,
      { cached: true, tier: "small" },
    );
    const figure = await screen.findByRole("group", { name: "Round trip and loss" });
    expect(figure).toHaveTextContent("12.3 ms · 0.0% loss");
    expect(figure.closest('[data-stale="true"]')).not.toBeNull();
    const state = screen.getByText("DOWN 10m");
    expect(state.className).toContain("text-crit-text");
    expect(screen.getByText("test-device")).toBeInTheDocument();
    // No sparkline, no subtitle.
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText(/no data since/)).not.toBeInTheDocument();
  });

  it("small: a failing disk is counted in red", async () => {
    renderCard(
      client,
      SmartDisksCard,
      (query) =>
        query.includes("smart_status")
          ? vector([
              [{ device: "sda" }, "1"],
              [{ device: "sdb" }, "0"],
            ])
          : vector([]),
      testInstance,
      { tier: "small" },
    );
    const figure = await screen.findByRole("group", { name: "Disk health" });
    expect(figure).toHaveTextContent("1 of 2 failing");
    expect(figure.className).toContain("text-crit-text");
  });

  it("medium: ping shows the figures, and asks for no sparkline history", async () => {
    const ranges: string[] = [];
    renderCard(
      client,
      PingCheckCard,
      (query, req) => {
        if (req.start !== undefined) ranges.push(query);
        return vector([[{}, query.includes("rtt") ? "0.0018" : "0"]]);
      },
      testInstance,
      { tier: "medium" },
    );
    const rtt = await screen.findByRole("group", { name: "Round trip" });
    expect(await within(rtt).findByText("1.8 ms")).toBeInTheDocument();
    expect(screen.queryByText("last hour")).not.toBeInTheDocument();
    expect(ranges).toEqual([]);
  });

  it("medium: a degraded RAID takes the disk tile, in red, so it is never clipped", async () => {
    renderCard(
      client,
      LinuxHostCard,
      (query) => {
        if (query.includes("node_md_degraded")) return vector([[{ device: "md0" }, "1"]]);
        if (query.includes("node_cpu_seconds_total")) return vector([[{}, "12"]]);
        if (query.includes("MemAvailable")) return vector([[{}, "40"]]);
        return vector([]);
      },
      testInstance,
      { tier: "medium" },
    );
    const raid = await screen.findByText("RAID md0");
    expect(raid.nextSibling).toHaveTextContent("degraded");
    expect((raid.nextSibling as HTMLElement).className).toContain("text-crit-text");
    expect(screen.queryByText("Disk")).not.toBeInTheDocument();
  });
});
