import { screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, vector } from "../testRender";
import { PfsenseOverviewCard } from "../pfsense-overview/PfsenseOverviewCard";
import { PfsenseGatewaysCard } from "./PfsenseGatewaysCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));
vi.mock("../metrics/TimeSeriesChart", () => ({ TimeSeriesChart: () => null }));

function answer(query: string) {
  if (query.includes("ping_percent_packet_loss"))
    return vector([
      [{ url: "192.0.2.10" }, "0"],
      [{ url: "192.0.2.254" }, "100"],
    ]);
  if (query.includes("ping_average_response_ms")) return vector([[{ url: "192.0.2.10" }, "0.564"]]);
  if (query.includes("cpu_usage_idle")) return vector([[{}, "87.5"]]);
  if (query.includes("mem_used_percent")) return vector([[{}, "33.7"]]);
  if (query.includes("pf_entries")) return vector([[{}, "13"]]);
  if (query.includes("net_bytes_recv")) return vector([[{ interface: "vtnet0" }, "1000000"]]);
  if (query.includes("net_bytes_sent")) return vector([[{ interface: "vtnet0" }, "2000"]]);
  return vector([]);
}

describe("pfSense cards", () => {
  it("overview shows CPU, memory, states and interface throughput", async () => {
    renderCard(client, PfsenseOverviewCard, answer);
    expect(await screen.findByText("13")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("34%")).toBeInTheDocument();
    expect(screen.getByText(/↓ 1\.0 Mbit\/s/)).toBeInTheDocument();
  });

  it("gateways: loss decides down, a down gateway without RTT is not missing", async () => {
    renderCard(client, PfsenseGatewaysCard, answer);
    expect(await screen.findByText("192.0.2.254")).toBeInTheDocument();
    expect(screen.getByText("down")).toBeInTheDocument();
    expect(screen.getByText("up")).toBeInTheDocument();
    expect(screen.getByText(/564 µs|0\.6 ms|0\.56 ms/)).toBeInTheDocument();
    expect(screen.getByText(/no reply/)).toBeInTheDocument();
  });

  it("say so when there is no data", async () => {
    renderCard(client, PfsenseGatewaysCard, () => vector([]));
    expect(await screen.findByText(/No gateway ping data/)).toBeInTheDocument();
  });
});
