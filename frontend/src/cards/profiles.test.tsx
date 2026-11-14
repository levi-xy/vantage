import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ComponentType } from "react";
import { ApiClient } from "../api";
import type { Instance, QueryResult } from "../api";
import { json } from "../test-utils";
import { CloudflareTunnelsCard } from "./cloudflare-tunnels/CloudflareTunnelsCard";
import { CloudflareZonesCard } from "./cloudflare-zones/CloudflareZonesCard";
import { CloudflaredCard } from "./cloudflared/CloudflaredCard";
import { EmbyLibrariesCard } from "./emby-libraries/EmbyLibrariesCard";
import { EmbyNowPlayingCard } from "./emby-now-playing/EmbyNowPlayingCard";
import { EmbyOverviewCard } from "./emby-overview/EmbyOverviewCard";
import type { CardProps } from "./registry";
import { CardTierContext } from "./metrics/tier";
import type { CardTier } from "./metrics/tier";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../app/session", () => ({ useApiClient: () => client, useCanEditDashboards: () => true }));

const instance = {
  id: "inst-1",
  name: "example",
  status: { state: "up", checked_at: null, last_success_at: null, error: null },
} as Instance;

type Row = [Record<string, string>, string];

function vector(rows: Row[]): QueryResult {
  return {
    result_type: "vector",
    series: rows.map(([labels, v]) => ({ labels, values: [[1_700_000_000, v]] })),
    warnings: [],
  };
}

function renderCard(
  Card: ComponentType<CardProps>,
  answer: (query: string) => Row[],
  tier: CardTier = "full",
) {
  const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
    const { query } = JSON.parse(String(init?.body)) as { query: string };
    return json(vector(answer(query)));
  });
  vi.spyOn(client, "query").mockImplementation((req) =>
    new ApiClient({ fetch: fetchMock }).query(req),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <CardTierContext.Provider value={{ tier }}>
        <Card instanceId="inst-1" settings={{}} title="Card" instance={instance} />
      </CardTierContext.Provider>
    </QueryClientProvider>,
  );
}

const ZONE = "a".repeat(32);
const ZONE_B = "b".repeat(32);

describe("profile cards", () => {
  it("cloudflared shows connections", async () => {
    renderCard(CloudflaredCard, (q) =>
      q.startsWith("cloudflared_tunnel_ha") ? [[{}, "4"]] : [[{}, "1"]],
    );
    expect(await screen.findByText("Edge connections")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("cloudflare zones names zones and says analytics unavailable, not zero", async () => {
    renderCard(CloudflareZonesCard, (q) => {
      if (q.includes("zone_info"))
        return [
          [{ zone_id: ZONE, zone_name: "example.com" }, "1"],
          [{ zone_id: ZONE_B, zone_name: "example.net" }, "1"],
        ];
      if (q.includes("analytics_up")) return [[{}, "0"]];
      return [
        [{ __name__: "vantage_cloudflare_zone_requests", zone_id: ZONE }, "1200"],
        [{ __name__: "vantage_cloudflare_zone_cached_requests", zone_id: ZONE }, "600"],
      ];
    });
    expect(await screen.findByText("example.com")).toBeInTheDocument();
    expect(screen.getByText("1,200")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByText("Analytics unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Analytics incomplete/)).toBeInTheDocument();
  });

  it("cloudflare tunnels shows name and status", async () => {
    renderCard(CloudflareTunnelsCard, (q) => {
      const id = "11111111-1111-1111-1111-111111111111";
      if (q.includes("tunnel_info"))
        return [[{ tunnel_id: id, tunnel_name: "example-tunnel" }, "1"]];
      if (q.includes("tunnel_status")) return [[{ tunnel_id: id, status: "degraded" }, "1"]];
      return [[{ tunnel_id: id }, "2"]];
    });
    expect(await screen.findByText("example-tunnel")).toBeInTheDocument();
    expect(screen.getByText("degraded")).toBeInTheDocument();
    expect(screen.getByText("2 connections")).toBeInTheDocument();
  });

  it("emby now playing lists who plays what, up to 4 rows", async () => {
    const sessions = ["alice", "bob", "carol", "dave", "erin"].map((u) => ({
      user: `example-${u}`,
      title: `Title of ${u}`,
    }));
    vi.spyOn(client, "nowPlaying").mockResolvedValue({
      checked_at: new Date().toISOString(),
      sessions,
    });
    renderCard(EmbyNowPlayingCard, () => []);
    const list = await screen.findByRole("list", { name: "Now playing" });
    expect(list).toHaveTextContent("example-alice");
    expect(list).toHaveTextContent("Title of dave");
    expect(list).not.toHaveTextContent("example-erin");
    expect(list).toHaveTextContent("and 1 more");
  });

  it("emby now playing: nothing playing, and one session at small size", async () => {
    vi.spyOn(client, "nowPlaying").mockResolvedValue({
      checked_at: new Date().toISOString(),
      sessions: [],
    });
    renderCard(EmbyNowPlayingCard, () => []);
    expect(await screen.findByText("Nothing playing.")).toBeInTheDocument();
  });

  it("emby now playing small: the one session in a line", async () => {
    vi.spyOn(client, "nowPlaying").mockResolvedValue({
      checked_at: new Date().toISOString(),
      sessions: [{ user: "example-alice", title: "Example Movie" }],
    });
    renderCard(EmbyNowPlayingCard, () => [], "small");
    expect(await screen.findByRole("group", { name: "Now playing" })).toHaveTextContent(
      "example-alice: Example Movie",
    );
  });

  it("emby overview shows server details and counts", async () => {
    renderCard(EmbyOverviewCard, (q) => {
      if (q.includes("server_info"))
        return [[{ server_name: "example-emby", version: "4.8.0.0" }, "1"]];
      if (q.includes("vantage_emby_streams")) return [[{ play_method: "transcode" }, "1"]];
      return [
        [{ __name__: "vantage_instance_up" }, "1"],
        [{ __name__: "vantage_emby_now_playing" }, "3"],
        [{ __name__: "vantage_emby_sessions" }, "5"],
        [{ __name__: "vantage_emby_update_available" }, "1"],
      ];
    });
    expect(await screen.findByText("Server up")).toBeInTheDocument();
    expect(screen.getByText("example-emby")).toBeInTheDocument();
    expect(screen.getByText("v4.8.0.0")).toBeInTheDocument();
    expect(screen.getByText("Update available")).toBeInTheDocument();
  });

  it("emby libraries shows item counts", async () => {
    renderCard(EmbyLibrariesCard, (q) =>
      q.includes("items")
        ? [[{ item_type: "movie" }, "1234"]]
        : [[{ library_type: "movies" }, "2"]],
    );
    expect(await screen.findByText("1,234")).toBeInTheDocument();
    expect(screen.getByText(/2 movies/)).toBeInTheDocument();
  });

  it("small: one figure each; a tunnel down and no edge connection in red", async () => {
    renderCard(
      CloudflaredCard,
      (q) => (q.startsWith("cloudflared_tunnel_ha") ? [[{}, "0"]] : [[{}, "1"]]),
      "small",
    );
    const conns = await screen.findByRole("group", { name: "Edge connections" });
    expect(conns).toHaveTextContent("0 edge connections");
    expect(conns.className).toContain("text-crit-text");
  });

  it("small tunnels: the count not healthy", async () => {
    renderCard(
      CloudflareTunnelsCard,
      (q) =>
        q.includes("status")
          ? [[{ tunnel_id: "t1", status: "down" }, "1"]]
          : [
              [{ tunnel_id: "t1", tunnel_name: "home" }, "1"],
              [{ tunnel_id: "t2", tunnel_name: "lab" }, "1"],
            ],
      "small",
    );
    expect(await screen.findByRole("group", { name: "Tunnels" })).toHaveTextContent(
      "1 of 2 not healthy",
    );
  });

  it.each([
    ["cloudflared", CloudflaredCard],
    ["cloudflare zones", CloudflareZonesCard],
    ["cloudflare tunnels", CloudflareTunnelsCard],
    ["emby overview", EmbyOverviewCard],
    ["emby libraries", EmbyLibrariesCard],
  ])("%s says so when there is no data", async (_name, Card) => {
    renderCard(Card, () => []);
    expect(await screen.findByText(/No .* (yet|reported|counts)|No library/)).toBeInTheDocument();
  });
});
