import type { Instance } from "../../api";
import { formatBytesPerSecond } from "../metrics/format";
import { testInstance, vector } from "../testRender";
import {
  historyQuery,
  portOptions,
  portsFromSettings,
  rateQuery,
  settingsWithPorts,
} from "./ports";

const pf: Instance = {
  ...testInstance,
  id: "pf",
  name: "firewall",
  config: { interface_names: ["igc0=WAN"], hidden_interfaces: ["igc3"] },
};
const sw: Instance = { ...testInstance, id: "sw", name: "switch" };

describe("throughput ports", () => {
  it("stores the chosen ports in card settings and reads them back", () => {
    const ports = [
      { source: "pfsense" as const, instanceId: "pf", iface: "igc0" },
      { source: "snmp" as const, instanceId: "sw", iface: "port3" },
    ];
    const settings = settingsWithPorts({ port9: "kept", other: 1 }, ports);
    expect(settings).toEqual({
      port9: "kept",
      other: 1,
      port1: "pfsense:pf:igc0",
      port2: "snmp:sw:port3",
    });
    expect(portsFromSettings(settings)).toEqual(ports);
    expect(portsFromSettings({ port1: "bogus:pf:igc0", port2: "pfsense:pf" })).toEqual([]);
  });

  it("builds a summed rate query and escapes device names", () => {
    const q = rateQuery([{ source: "node", instanceId: "h", iface: 'en"0\\' }], "in");
    expect(q).toBe(
      '8 * ((sum(rate(node_network_receive_bytes_total{vantage_instance_id="h",device="en\\"0\\\\"}[2m])) or vector(0)))',
    );
    expect(rateQuery([], "out")).toBe("vector(0)");
  });

  it("leaves a time with no report empty in the history query, never 0", () => {
    const q = historyQuery(
      [
        { source: "pfsense", instanceId: "pf", iface: "igc0" },
        { source: "node", instanceId: "h", iface: "eth0" },
      ],
      "out",
    );
    expect(q).toBe(
      '8 * sum(rate(net_bytes_sent{vantage_instance_id="pf",interface="igc0"}[2m]) or rate(node_network_transmit_bytes_total{vantage_instance_id="h",device="eth0"}[2m]))',
    );
    expect(q).not.toContain("vector(0)");
  });

  it("names ports as the cards do and leaves hidden pfSense interfaces out", () => {
    const groups = portOptions(
      {
        pfsense: vector([
          [{ vantage_instance_id: "pf", interface: "igc0" }, "1"],
          [{ vantage_instance_id: "pf", interface: "igc3" }, "1"],
        ]),
        snmp: vector([
          [{ vantage_instance_id: "sw", if_name: "port10" }, "1"],
          [{ vantage_instance_id: "sw", if_name: "port2" }, "1"],
          [{ vantage_instance_id: "gone", if_name: "port1" }, "1"],
        ]),
      },
      vector([[{ vantage_instance_id: "sw", if_name: "port2", if_label: "NAS" }, "1"]]),
      [pf, sw],
    );
    expect(groups.map((g) => [g.instance.name, g.ports.map((p) => [p.name, p.detail])])).toEqual([
      ["firewall", [["WAN", "igc0"]]],
      [
        "switch",
        [
          ["NAS", "port2"],
          ["port10", undefined],
        ],
      ],
    ]);
  });

  it("names pfSense ports by the description set on pfSense", () => {
    const groups = portOptions(
      { pfsense: vector([[{ vantage_instance_id: "pf", interface: "igc1" }, "1"]]) },
      undefined,
      [pf],
      vector([[{ vantage_instance_id: "pf", interface: "igc1", descr: "LAN" }, "1"]]),
    );
    expect(groups[0]?.ports.map((p) => [p.name, p.detail])).toEqual([["LAN", "igc1"]]);
  });

  it("formats rates in bytes when asked", () => {
    expect(formatBytesPerSecond(8_000_000)).toBe("1.0 MB/s");
  });
});

describe("historyQuery rate window", () => {
  it("uses the given rate() range", () => {
    const q = historyQuery([{ source: "pfsense", instanceId: "pf", iface: "igc0" }], "in", "1512s");
    expect(q).toContain("[1512s]");
  });
});
