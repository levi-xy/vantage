import { isPhysicalBsd, isPhysicalLinux, isPhysicalSnmp } from "./physicalPorts";

describe("physical ports", () => {
  it("tells FreeBSD NICs from VLANs, tunnels and laggs", () => {
    for (const name of ["igc0", "igc1", "em1", "ix0", "vtnet0", "re0", "ixl0", "bge0", "ue0"]) {
      expect(isPhysicalBsd(name), name).toBe(true);
    }
    const logical = ["igc1.10", "lo0", "pflog0", "pfsync0", "enc0", "wg0", "ovpns1", "ovpnc2"];
    for (const name of [...logical, "lagg0", "bridge0", "gif0", "tun1", "ppp0", "wlan0"]) {
      expect(isPhysicalBsd(name), name).toBe(false);
    }
  });

  it("tells Linux NICs from virtual ones", () => {
    for (const name of ["eth0", "eno1", "enp3s0", "ens18", "em1", "ib0"]) {
      expect(isPhysicalLinux(name), name).toBe(true);
    }
    const virtual = ["lo", "eth0.10", "veth1a2b@if3", "docker0", "br-1a2b", "virbr0", "vmbr0"];
    for (const name of [...virtual, "bond0", "tailscale0", "wg0", "wlp2s0", "flannel.1"]) {
      expect(isPhysicalLinux(name), name).toBe(false);
    }
  });

  it("goes by ifType and ifConnectorPresent for SNMP, and keeps what has no ifType", () => {
    const base = { id: "port1", internal: false };
    expect(isPhysicalSnmp({ ...base, ifType: "6", connector: "true" })).toBe(true);
    expect(isPhysicalSnmp({ ...base, ifType: "117", connector: "" })).toBe(true);
    expect(isPhysicalSnmp({ ...base, ifType: "209" })).toBe(false); // bridge
    expect(isPhysicalSnmp({ ...base, ifType: "135" })).toBe(false); // VLAN
    expect(isPhysicalSnmp({ ...base, ifType: "6", connector: "false" })).toBe(false);
    expect(isPhysicalSnmp({ ...base, ifType: "" })).toBe(true);
    expect(isPhysicalSnmp({ ...base, internal: true })).toBe(false);
    expect(isPhysicalSnmp({ id: "Gi1/0/1.100", ifType: "6", internal: false })).toBe(false);
  });
});
