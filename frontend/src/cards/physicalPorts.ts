// Which interfaces belong on a faceplate (the map's panel read from the device, the
// dashboard's switch front panel): physical ports only. VLAN sub-interfaces, tunnels,
// bridges and other logical interfaces carry useful traffic, so interface lists still show
// them; only faceplates filter. One rule per source, shared by the map and the cards.

// ifType values of wired Ethernet front-panel ports: ethernetCsmacd, fastEther, fastEtherFX,
// gigabitEthernet. Wireless (71) is no front-panel port.
const ETHERNET_IF_TYPES = new Set([6, 62, 69, 117]);

/** An SNMP interface. `ifType` and `connector` are "" (or absent) when the agent has none. */
export function isPhysicalSnmp(port: {
  id: string;
  ifType?: string;
  connector?: string;
  /** Name-based fallback (loopbacks, tunnels) for agents that report no ifType. */
  internal: boolean;
}): boolean {
  if (port.id.includes(".")) return false; // a sub-interface (Gi1/0/1.100, eth0.10)
  // The connector flag is unreliable (some agents say false for an empty SFP cage), so a
  // physical ifType alone decides; the flag only breaks the tie when there is no ifType.
  if (port.ifType) return ETHERNET_IF_TYPES.has(Number(port.ifType));
  return port.connector === "true" || !port.internal; // else keep today's behaviour
}

// FreeBSD (pfSense, OPNsense): logical interface families, each followed by a unit number.
const BSD_LOGICAL =
  /^(lo|pflog|pfsync|enc|ovpn[sc]?|wg|tun|tap|gif|gre|lagg|bridge|vlan|qinq|ipsec|l2tp|pppoe|ppp|epair|stf|wlan)\d*$/;

/** A physical FreeBSD NIC: driver name + unit (igc0, em1, ix0, vtnet0, ue0). */
export function isPhysicalBsd(name: string): boolean {
  return /^[a-z]+\d+$/.test(name) && !name.includes(".") && !BSD_LOGICAL.test(name);
}

const LINUX_LOGICAL =
  /^(lo|veth|docker|br-|virbr|vmbr|bond|team|tap|tun|wg|tailscale|zt|cni|flannel|cali|vxlan|kube)/;

/** A wired Linux NIC (eth0, eno1, enp3s0, ens18, em1, ib0); not wireless, not virtual. */
export function isPhysicalLinux(name: string): boolean {
  if (name.includes(".") || name.includes("@") || LINUX_LOGICAL.test(name)) return false;
  return /^(eth|em|ib)\d+$/.test(name) || /^en([a-z]\w*|\d+)$/.test(name);
}
