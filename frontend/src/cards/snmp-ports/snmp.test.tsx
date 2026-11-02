import { fireEvent, screen } from "@testing-library/react";
import { ApiClient } from "../../api";
import { renderCard, vector } from "../testRender";
import { SnmpOverviewCard } from "../snmp-overview/SnmpOverviewCard";
import { SwitchPanelCard } from "../switch-panel/SwitchPanelCard";
import { isInternal } from "./ports";
import { SnmpPortsCard } from "./SnmpPortsCard";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../../app/session", () => ({ useApiClient: () => client }));

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
    expect(screen.getByText(/· up 1d 1h/)).toBeInTheDocument(); // in the header
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

  it("hide internal interfaces by default and count physical ports only", async () => {
    const withInternal = (query: string) => {
      const extra = (v: string) =>
        [
          [{ if_index: "10", if_name: "lo" }, v],
          [{ if_index: "11", if_name: "pim6reg" }, v],
        ] as [Record<string, string>, string][];
      const base = answer(query);
      if (query.includes("if_oper_status") || query.includes("if_admin_status"))
        return { ...base, series: [...base.series, ...vector(extra("1")).series] };
      return base;
    };
    renderCard(client, SnmpPortsCard, withInternal);
    expect(await screen.findByText(/2 internal interfaces hidden/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "lo: up" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByRole("button", { name: "lo: up" })).toBeInTheDocument();
  });

  it("overview counts physical ports only", async () => {
    renderCard(client, SnmpOverviewCard, (query) =>
      query.includes("if_oper_status")
        ? vector([
            [port("1"), "1"],
            [{ if_index: "10", if_name: "lo" }, "1"],
          ])
        : answer(query),
    );
    expect(await screen.findByText("Interfaces up")).toBeInTheDocument();
    expect(screen.getByText("Interfaces up").nextSibling).toHaveTextContent("1");
    expect(screen.getByText(/1 internal .* not counted/)).toBeInTheDocument();
  });

  it("tells internal interfaces by name", () => {
    for (const id of ["lo", "lo0", "Loopback0", "pimreg", "pim6reg", "tun0", "gre0", "dummy0"])
      expect(isInternal({ id, name: id })).toBe(true);
    for (const id of ["eth0", "port3", "sfpplus25", "ether1", "bond0", "br0", "green"])
      expect(isInternal({ id, name: id })).toBe(false);
  });

  it("switch panel fills the detail strip with the tapped port", async () => {
    renderCard(client, SwitchPanelCard, answer);
    fireEvent.click(await screen.findByRole("button", { name: "port1: up" }));
    expect(screen.getByText(/^Port 1 · port1 · uplink to core · 1/)).toBeInTheDocument();
    expect(screen.getByText("Ports up").nextSibling).toHaveTextContent("1 of 3");
    expect(screen.getByText("Busiest").nextSibling).toHaveTextContent("port1 · 250 kbit/s");
  });

  it("say so when there is no data", async () => {
    renderCard(client, SnmpPortsCard, () => vector([]));
    expect(await screen.findByText(/No data yet/)).toBeInTheDocument();
  });
});
