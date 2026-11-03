import { fireEvent, screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, testInstance, vector } from "../testRender";
import { interfaceRows } from "../pfsense-interfaces/interfaceRows";
import { PfsenseInterfacesCard } from "../pfsense-interfaces/PfsenseInterfacesCard";
import { PfsenseOverviewCard } from "../pfsense-overview/PfsenseOverviewCard";
import { PfsenseGatewaysCard } from "./PfsenseGatewaysCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

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
  if (query.includes("disk_used_percent")) return vector([[{ path: "/" }, "4.8"]]);
  if (query.includes("system_uptime")) return vector([[{}, "90000"]]);
  if (query.includes("net_bytes_recv"))
    return vector([
      [{ interface: "vtnet0" }, "1000000"],
      [{ interface: "vtnet1" }, "5000"],
      [{ interface: "vtnet2" }, "7000"],
      [{ interface: "vtnet1.10" }, "3000"],
      [{ interface: "vtnet3" }, "20000000"],
      [{ interface: "vtnet4" }, "100"],
      [{ interface: "vtnet5" }, "50"],
      [{ interface: "vtnet6" }, "10"],
    ]);
  if (query.includes("net_bytes_sent")) return vector([[{ interface: "vtnet0" }, "2000"]]);
  return vector([]);
}

const pfsense = { ...testInstance, type_id: "pfsense" };

describe("pfSense cards", () => {
  it("resources shows CPU, memory, storage, states, and uptime in the header", async () => {
    renderCard(client, PfsenseOverviewCard, answer, pfsense);
    expect(await screen.findByText("13")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("34%")).toBeInTheDocument();
    expect(await screen.findByText("4.8%")).toBeInTheDocument();
    expect(await screen.findByText("pfSense · up 1d 1h")).toBeInTheDocument();
    expect(screen.queryByText("Temperature")).not.toBeInTheDocument(); // no sensor
    expect(screen.queryByText(/Mbit\/s/)).not.toBeInTheDocument();
  });

  it("interfaces: WAN pinned, busiest first, VLAN under its parent, six rows then show all", async () => {
    const instance = {
      ...pfsense,
      config: { interface_names: ["vtnet0=WAN"], hidden_interfaces: ["vtnet2"] },
    };
    renderCard(client, PfsenseInterfacesCard, answer, instance);
    const list = await screen.findByRole("list", { name: "Interfaces" });
    const names = () =>
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.querySelector("span span:not([aria-hidden])")?.textContent);
    // WAN (vtnet0) pinned above the busier vtnet3; vtnet1.10 under vtnet1; vtnet2 hidden.
    expect(names()).toEqual(["WAN", "vtnet3", "vtnet1", "vtnet1.10", "vtnet4", "vtnet5"]);
    expect(within(list).getAllByRole("listitem")[3]).toHaveAttribute("data-child", "true");
    expect(within(list).getByText("vtnet0")).toBeInTheDocument(); // OS name next to WAN
    expect(screen.queryByText("vtnet2")).not.toBeInTheDocument();
    expect(screen.queryByText("vtnet6")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show all 7 interfaces" }));
    expect(screen.getByText("vtnet6")).toBeInTheDocument();
    expect(screen.queryByText("vtnet2")).not.toBeInTheDocument();
  });

  it("interfaces: pfSense's own descriptions, a name set in Vantage wins", async () => {
    const id = pfsense.id;
    const withInfo = (query: string) =>
      query.includes("pfsense_interface_info")
        ? vector([
            [{ vantage_instance_id: id, interface: "vtnet0", descr: "WAN" }, "1"],
            [{ vantage_instance_id: id, interface: "vtnet3", descr: "IoT" }, "1"],
            [{ vantage_instance_id: id, interface: "vtnet4", descr: "LAN" }, "1"],
          ])
        : answer(query);
    const instance = { ...pfsense, config: { interface_names: ["vtnet4=Office"] } };
    renderCard(client, PfsenseInterfacesCard, withInfo, instance);
    const list = await screen.findByRole("list", { name: "Interfaces" });
    expect(await within(list).findByText("IoT")).toBeInTheDocument();
    // WAN from the description is still pinned first.
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("WAN");
    expect(within(list).getByText("Office")).toBeInTheDocument();
    expect(within(list).queryByText("LAN")).not.toBeInTheDocument();
    expect(within(list).getByText("vtnet3")).toBeInTheDocument(); // OS name kept beside it
  });

  it("interface rows: flat without parents, nothing pinned without a WAN", () => {
    const rows = interfaceRows([
      { id: "igc1.20", name: "igc1.20", rx: 5, tx: 5 },
      { id: "igc2", name: "LAN", rx: 1, tx: 1 },
    ]);
    expect(rows.map((r) => [r.id, r.child, r.pinned])).toEqual([
      ["igc1.20", false, false],
      ["igc2", false, false],
    ]);
  });

  it("gateways: friendly names, loss decides down, healthy rows plain", async () => {
    const instance = { ...pfsense, config: { gateway_names: ["192.0.2.10=WAN_DHCP"] } };
    renderCard(client, PfsenseGatewaysCard, answer, instance);
    expect(await screen.findByText("WAN_DHCP")).toBeInTheDocument();
    expect(screen.getByText("192.0.2.10")).toBeInTheDocument(); // the address, small
    expect(screen.getByText("192.0.2.254")).toBeInTheDocument(); // no name: the address
    const healthy = screen.getByText(/0\.6 ms · 0\.0% loss/);
    expect(healthy.className).toContain("text-muted");
    expect(healthy.className).not.toMatch(/emerald|crit/);
    expect(screen.getByText(/no reply · 100% loss/).className).toContain("text-crit-text");
    expect(screen.queryByText("up")).not.toBeInTheDocument(); // no badges
  });

  it("small gateways: the worst one, in red", async () => {
    renderCard(client, PfsenseGatewaysCard, answer, pfsense, { tier: "small" });
    const worst = await screen.findByRole("group", { name: "Worst gateway: 192.0.2.254" });
    expect(worst).toHaveTextContent("no reply · 100%");
    expect(worst.className).toContain("text-crit-text");
    expect(screen.queryByRole("list", { name: "Gateways" })).not.toBeInTheDocument();
  });

  it("medium interfaces: the top 3, no bars or show all", async () => {
    const instance = { ...pfsense, config: { interface_names: ["vtnet0=WAN"] } };
    renderCard(client, PfsenseInterfacesCard, answer, instance, { tier: "medium" });
    const list = await screen.findByRole("list", { name: "Interfaces" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("WAN");
    expect(screen.queryByRole("button", { name: /Show all/ })).not.toBeInTheDocument();
  });

  it("say so when there is no data", async () => {
    renderCard(client, PfsenseGatewaysCard, () => vector([]));
    expect(await screen.findByText(/No gateway ping data/)).toBeInTheDocument();
  });
});
