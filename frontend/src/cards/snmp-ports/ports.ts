// Port data shared by the SNMP cards (Ports, Switch panel): one row per interface.
import type { QueryResult } from "../../api";
import { formatBitsPerSecond } from "../metrics/format";
import { cardFreshness } from "../metrics/freshness";
import { parseValue } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import { isPhysicalSnmp } from "../physicalPorts";
import { IF_DOWN, IF_UP, snmpMetric } from "../snmp-overview/snmpMetrics";

export type PortState = "up" | "down" | "disabled";

export interface Port {
  index: string;
  /** The stable port id from the metric labels (if_name: port3, sfpplus9, eth0). */
  id: string;
  /** What to show: the name set on the device when there is one, else the id. */
  name: string;
  alias: string;
  state: PortState;
  speed?: number;
  inBps?: number;
  outBps?: number;
  /** A front-panel port (not a VLAN, tunnel or other logical interface), by ifType. */
  physical: boolean;
}

/**
 * A port tile by state, as on the switch faceplate: link up = tinted fill with the ok
 * border, no link = a neutral outline, disabled = a dashed neutral outline. No link is not
 * an alarm (most switches have unused ports), so it is never red.
 */
export const TILE: Record<PortState, string> = {
  up: "border-ok bg-ok/12 text-ink",
  down: "border-port-off text-muted",
  disabled: "border-dashed border-port-off text-muted",
};

/** A port's state in words where a tile shows it (an up port shows its speed). */
export const STATE_TEXT: Record<PortState, string> = {
  up: "",
  down: "no link",
  disabled: "disabled",
};

/** The faceplate notch of a port tile, coloured like its border. */
export const NOTCH: Record<PortState, string> = {
  up: "bg-ok",
  down: "bg-port-off",
  disabled: "bg-port-off",
};

/** What each tile style means, for a legend. */
export const LEGEND: { state: PortState; label: string }[] = [
  { state: "up", label: "Link up" },
  { state: "down", label: "No link" },
  { state: "disabled", label: "Disabled" },
];

// Interfaces that are not a physical port: loopback, multicast registers, tunnels and other
// virtual devices. IF-MIB's ifType would say so directly, but the integrations don't
// collect it yet, so this goes by the interface name (Linux, VyOS, Cisco-style names).
const INTERNAL_NAMES = [
  /^lo(opback)?[\d_.:-]*$/i, // lo, lo0, Loopback0
  /^software loopback/i, // Windows ifDescr
  /^null\d*$/i, // Null0
  /^pim6?reg/i, // pimreg, pim6reg
  /^tun/i, // tun0, tunl0, tunnel1
  /^(ip6)?gre(tap)?\d*$/i, // gre0, gretap0, ip6gre0
  /^erspan/i,
  /^ip6?tnl/i, // ip6tnl0
  /^ip6?_vti/i,
  /^sit\d/i,
  /^dummy/i,
  /^ifb\d/i,
  /^teql\d/i,
];

/** A loopback, tunnel or other virtual interface rather than a physical port. */
export function isInternal(port: Pick<Port, "id" | "name">): boolean {
  const name = port.id || port.name;
  return INTERNAL_NAMES.some((re) => re.test(name));
}

/** The port with the most traffic (in + out), if any carries traffic. */
export function busiest(ports: Port[]): { port: Port; total: number } | undefined {
  let best: { port: Port; total: number } | undefined;
  for (const port of ports) {
    const total = (port.inBps ?? 0) + (port.outBps ?? 0);
    if (total > 0 && (!best || total > best.total)) best = { port, total };
  }
  return best;
}

/** Value per `if_index`. */
function byIndex(result: QueryResult | undefined): Map<string, number | undefined> {
  const out = new Map<string, number | undefined>();
  for (const s of result?.series ?? []) {
    out.set(s.labels.if_index ?? "", parseValue(s.values[0]?.[1]));
  }
  return out;
}

export function speedText(bps: number | undefined): string {
  return bps ? formatBitsPerSecond(bps) : "–";
}

export function describe(p: Port, formatRate: (bps: number | undefined) => string): string {
  const name = p.alias ? `${p.name} (${p.alias})` : p.name;
  return `${name}: ${p.state}, ${speedText(p.speed)}, in ${formatRate(p.inBps)}, out ${formatRate(p.outBps)}`;
}

/** Every port of an SNMP instance, sorted by ifIndex, with state, speed and rates. */
export function useSnmpPorts(instanceId: string) {
  const oper = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_oper_status"));
  const admin = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_admin_status"));
  const speed = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_speed_bits_per_second"));
  const rx = useInstantQuery(
    instanceId,
    (sel) => `8 * rate(${snmpMetric(sel, "if_in_bytes_total")}[5m])`,
  );
  const tx = useInstantQuery(
    instanceId,
    (sel) => `8 * rate(${snmpMetric(sel, "if_out_bytes_total")}[5m])`,
  );
  // The interface alias is free text, so it is only on the info series: join on if_index.
  const info = useInstantQuery(instanceId, (sel) => snmpMetric(sel, "if_info"));

  const adminBy = byIndex(admin.data);
  const speedBy = byIndex(speed.data);
  const rxBy = byIndex(rx.data);
  const txBy = byIndex(tx.data);
  const infoBy = new Map(
    (info.data?.series ?? []).map((s) => [s.labels.if_index ?? "", s.labels] as const),
  );

  const ports: Port[] = (oper.data?.series ?? [])
    .map((s): Port => {
      const index = s.labels.if_index ?? "";
      const operValue = parseValue(s.values[0]?.[1]);
      const state: PortState =
        adminBy.get(index) === IF_DOWN ? "disabled" : operValue === IF_UP ? "up" : "down";
      const labels = infoBy.get(index);
      const id = s.labels.if_name ?? labels?.if_name ?? "";
      return {
        index,
        id,
        physical: isPhysicalSnmp({
          id,
          ifType: labels?.if_type,
          connector: labels?.connector,
          internal: isInternal({ id, name: id }),
        }),
        // A MikroTik port's own name (if_label) is display text on the info series only.
        name: labels?.if_label || s.labels.if_name || labels?.if_name || `if${index}`,
        alias: labels?.if_alias ?? "",
        state,
        speed: speedBy.get(index),
        inBps: rxBy.get(index),
        outBps: txBy.get(index),
      };
    })
    .sort((a, b) => Number(a.index) - Number(b.index));
  const freshness = cardFreshness(oper, admin, speed, rx, tx, info);
  return { ports, loading: oper.isPending, error: oper.error, freshness };
}
