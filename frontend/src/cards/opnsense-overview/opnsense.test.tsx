import { screen, within } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, testInstance, vector } from "../testRender";
import { OpnsenseGatewaysCard } from "../opnsense-gateways/OpnsenseGatewaysCard";
import { OpnsenseInterfacesCard } from "../opnsense-interfaces/OpnsenseInterfacesCard";
import { OpnsenseServicesCard } from "../opnsense-services/OpnsenseServicesCard";
import { OpnsenseOverviewCard } from "./OpnsenseOverviewCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({
  useApiClient: () => client,
  useCanEditDashboards: () => true,
}));

const opnsense = { ...testInstance, type_id: "opnsense" };
const gw = (gateway: string) => ({ gateway });
const ifc = (interfaceName: string) => ({ interface: interfaceName });

function answer(query: string) {
  if (query.includes("load1")) return vector([[{}, "0.25"]]);
  if (query.includes("memory_used_bytes")) return vector([[{}, "25"]]);
  if (query.includes("pf_states")) return vector([[{}, "1234"]]);
  if (query.includes("uptime_seconds")) return vector([[{}, "90000"]]);
  if (query.includes("opnsense_info")) return vector([[{ version: "OPNsense 24.7.5-amd64" }, "1"]]);
  if (query.includes("gateway_up"))
    return vector([
      [gw("WAN_DHCP"), "1"],
      [gw("BACKUP_GW"), "0"],
    ]);
  if (query.includes("gateway_delay")) return vector([[gw("WAN_DHCP"), "0.0043"]]);
  if (query.includes("gateway_loss")) return vector([[gw("BACKUP_GW"), "100"]]);
  if (query.includes("interface_up"))
    return vector([
      [ifc("vtnet1"), "0"],
      [ifc("vtnet0"), "1"],
    ]);
  if (query.includes("interface_info"))
    return vector([[{ ...ifc("vtnet0"), interface_name: "WAN" }, "1"]]);
  if (query.includes("receive_bytes")) return vector([[ifc("vtnet0"), "2000000"]]);
  if (query.includes("transmit_bytes")) return vector([[ifc("vtnet0"), "500000"]]);
  if (query.includes("service_running"))
    return vector([
      [{ service: "unbound" }, "1"],
      [{ service: "dhcpd" }, "0"],
    ]);
  return vector([]);
}

describe("OPNsense cards", () => {
  it("overview shows load, memory, states, firmware and uptime", async () => {
    renderCard(client, OpnsenseOverviewCard, answer, opnsense);
    expect(await screen.findByText("1234")).toBeInTheDocument();
    expect(screen.getByText("25%")).toBeInTheDocument();
    expect(screen.getByText("24.7.5-amd64")).toBeInTheDocument();
    expect(await screen.findByText("OPNsense · up 1d 1h")).toBeInTheDocument();
  });

  it("interfaces: up first, named from the info series, with rates", async () => {
    renderCard(client, OpnsenseInterfacesCard, answer, opnsense);
    const list = await screen.findByRole("list", { name: "Interfaces" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("WAN");
    expect(items[0]).toHaveTextContent("vtnet0");
    expect(items[0]).toHaveTextContent("in 2.0 Mbit/s");
    expect(items[1]).toHaveTextContent("vtnet1");
    expect(items[1]).toHaveTextContent("down");
  });

  it("gateways: a down gateway is first and flagged", async () => {
    renderCard(client, OpnsenseGatewaysCard, answer, opnsense);
    const list = await screen.findByRole("list", { name: "Gateways" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("BACKUP_GW");
    expect(items[0]).toHaveTextContent("down");
    expect(items[1]).toHaveTextContent("4.3 ms");
  });

  it("services: stopped ones first", async () => {
    renderCard(client, OpnsenseServicesCard, answer, opnsense);
    const list = await screen.findByRole("list", { name: "Services" });
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("dhcpd");
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("stopped");
  });
});
