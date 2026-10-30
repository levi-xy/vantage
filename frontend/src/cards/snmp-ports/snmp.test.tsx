import { screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, vector } from "../testRender";
import { SnmpOverviewCard } from "../snmp-overview/SnmpOverviewCard";
import { SnmpPortsCard } from "./SnmpPortsCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));
vi.mock("../metrics/TimeSeriesChart", () => ({ TimeSeriesChart: () => null }));

const port = (n: string) => ({ if_index: n, if_name: `port${n}` });
function answer(query: string) {
  const has = (s: string) => query.includes(s);
  if (!has('vantage_instance_id="inst-1"')) throw new Error("unscoped query");
  if (has("if_oper_status"))
    return vector([
      [port("1"), "1"],
      [port("2"), "2"],
      [port("3"), "2"],
    ]);
  if (has("if_admin_status"))
    return vector([
      [port("1"), "1"],
      [port("2"), "1"],
      [port("3"), "2"],
    ]);
  if (has("if_speed")) return vector([[port("1"), "1000000000"]]);
  if (has("if_in_bytes")) return vector([[port("1"), "125000"]]);
  if (has("if_out_bytes")) return vector([[port("1"), "125000"]]);
  if (has("if_info")) return vector([[{ ...port("1"), if_alias: "uplink to core" }, "1"]]);
  if (has("_info")) return vector([[{ sys_name: "switch-a", sys_descr: "Example switch" }, "1"]]);
  if (has("uptime")) return vector([[{}, "90000"]]);
  return vector([]);
}

describe("snmp cards", () => {
  it("overview shows the name, uptime and interface counts", async () => {
    renderCard(client, SnmpOverviewCard, answer);
    expect(await screen.findByText("switch-a")).toBeInTheDocument();
    expect(screen.getByText("1d 1h")).toBeInTheDocument();
    expect(screen.getByText("Interfaces down").nextSibling).toHaveTextContent("2");
  });

  it("ports colour tiles by status and show rates", async () => {
    renderCard(client, SnmpPortsCard, answer);
    expect(await screen.findByRole("button", { name: "port1: up" })).toHaveAttribute(
      "title",
      expect.stringContaining("uplink to core"),
    );
    expect(screen.getByRole("button", { name: "port2: down" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "port3: disabled" })).toBeInTheDocument();
    expect(screen.getByLabelText("Top talkers")).toHaveTextContent("250 kbit/s");
  });

  it("ports show a MikroTik port's own name from if_label", async () => {
    renderCard(client, SnmpPortsCard, (query) =>
      query.includes("if_info")
        ? vector([[{ ...port("1"), if_alias: "", if_label: "NAS uplink" }, "1"]])
        : answer(query),
    );
    expect(await screen.findByRole("button", { name: "NAS uplink: up" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "port2: down" })).toBeInTheDocument();
  });

  it("say so when there is no data", async () => {
    renderCard(client, SnmpPortsCard, () => vector([]));
    expect(await screen.findByText(/No data yet/)).toBeInTheDocument();
  });
});
