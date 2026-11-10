import { describe, expect, it } from "vitest";
import type { MapBox, MapPanel } from "../api";
import { faceplate } from "../cards/switch-panel/panel";
import type { DevicePort } from "./maps/devicePorts";
import { fromDevice } from "./maps/panel";
import { fetchSysDescr, layoutOnPreset, readDevicePanel } from "./maps/editor/presetLayout";
import {
  applyPreset,
  matchPreset,
  presetGroups,
  presetLabel,
  PRESETS,
} from "./maps/editor/presets";
import type { Preset } from "./maps/editor/presets";

const preset = (id: string): Preset => {
  const found = PRESETS.find((p) => p.id === id);
  if (!found) throw new Error(`preset ${id} missing`);
  return found;
};

// The maintainer's CRS328 on SwOS: port1..port28, the last four are 10G cages.
const swos: DevicePort[] = Array.from({ length: 28 }, (_, i) => ({
  id: `port${i + 1}`,
  name: `Port${i + 1}`,
  state: "up",
  speed: i >= 24 ? 10e9 : 1e9,
  ref: { source: "snmp", instanceId: "sw", iface: `port${i + 1}` },
  order: i + 1,
  physical: true,
}));

const port = (id: string, order: number, source = "snmp", speed = 1e9): DevicePort => ({
  id,
  name: id,
  state: "up",
  speed,
  ref: { source, instanceId: "dev", iface: id } as DevicePort["ref"],
  order,
  physical: true,
});

describe("preset list", () => {
  it("matches a model in sysDescr, case-insensitively and on whole model names", () => {
    expect(matchPreset("RouterOS CRS328-24P-4S+")?.id).toBe("mikrotik-crs328-24p-4s");
    expect(matchPreset("swos crs328-24p-4s+rm")?.id).toBe("mikrotik-crs328-24p-4s");
    expect(matchPreset("GS308E - 8-Port Gigabit")?.id).toBe("netgear-gs308e");
    expect(matchPreset("GS308EP")).toBeUndefined();
    expect(matchPreset("Linux host 5.15")).toBeUndefined();
    expect(matchPreset(undefined)).toBeUndefined();
  });

  it("groups by vendor with Generic last, and marks unverified models", () => {
    const groups = presetGroups().map((g) => g.vendor);
    expect(groups.at(-1)).toBe("Generic");
    expect(groups).toContain("MikroTik");
    expect(presetLabel(preset("mikrotik-hex-rb750gr3"))).toMatch(/\(unverified\)$/);
    expect(presetLabel(preset("mikrotik-crs328-24p-4s"))).not.toMatch(/unverified/);
    // Descending numbering can't be expressed, so those models are not offered.
    expect(PRESETS.some((p) => p.id === "ubiquiti-ucg-max" || p.id === "protectli-vp4630")).toBe(
      false,
    );
  });

  it("has a CRS328 preset that is exactly what the switch card's faceplate draws", () => {
    const p = preset("mikrotik-crs328-24p-4s");
    expect(p.groups).toEqual([
      { kind: "rj45", count: 8, rows: 2, order: "odd_bottom", first: 1, prefix: "ether" },
      { kind: "rj45", count: 8, rows: 2, order: "odd_bottom", first: 9, prefix: "ether" },
      { kind: "rj45", count: 8, rows: 2, order: "odd_bottom", first: 17, prefix: "ether" },
      { kind: "sfp_plus", count: 4, rows: 2, order: "odd_bottom", first: 1, prefix: "sfp-sfpplus" },
    ]);
    const plate = faceplate(swos);
    expect(plate.copper.map((b) => b.length)).toEqual([8, 8, 8]);
    expect(plate.sfp).toHaveLength(4);
    const read = fromDevice(swos, [], "all");
    expect(
      read.groups.map(({ kind, count, rows, order, first }) => ({
        kind,
        count,
        rows,
        order,
        first,
      })),
    ).toEqual(
      p.groups.map(({ kind, count, rows, order, first }) => ({ kind, count, rows, order, first })),
    );
  });

  it("copies a non-MikroTik model: UDM-Pro with 8 LAN in two rows, WAN and stacked SFP+", () => {
    const p = preset("ubiquiti-udm-pro");
    const box = { shape: "device" } as MapBox;
    const next = applyPreset(box, p).panel as MapPanel;
    expect(next.groups.map((g) => [g.kind, g.count, g.rows, g.order, g.first])).toEqual([
      ["rj45", 8, 2, "odd_top", 1],
      ["rj45", 1, 1, "rows", 9],
      ["sfp_plus", 2, 2, "rows", 10],
    ]);
    expect(next.ports).toHaveLength(11);
  });
});

describe("read from the device with a known model", () => {
  it("leaves the CRS328 on SwOS exactly as the plain read draws it", () => {
    const plain = fromDevice(swos, [], "all");
    const known = layoutOnPreset(preset("mikrotik-crs328-24p-4s"), swos, [], "all");
    expect(known).toEqual(plain);
  });

  it("puts RouterOS interfaces on the preset's groups by name and keeps the rest at the end", () => {
    const names = ["ether1", "ether2", "ether10", "sfp-sfpplus3", "bridge-lan-x", "wlan0"];
    const device = names.map((id, i) => port(id, i + 1, "snmp", id.startsWith("sfp") ? 10e9 : 1e9));
    const read = layoutOnPreset(preset("mikrotik-crs328-24p-4s"), device, [], "all");
    const at = (iface: string) => read.ports.find((p) => p.interface === iface);
    expect(at("ether1")).toMatchObject({ group: "g1", n: 1 });
    expect(at("ether10")).toMatchObject({ group: "g2", n: 2 });
    expect(at("sfp-sfpplus3")).toMatchObject({ group: "g4", n: 3 });
    // The preset's own ports stay even without a device port; the extras get a group.
    expect(read.groups.slice(0, 4).map((g) => g.count)).toEqual([8, 8, 8, 4]);
    expect(read.groups).toHaveLength(5);
    expect(at("wlan0")?.group).toBe("g5");
    expect(read.ports.filter((p) => p.group === "g1" && p.interface === "")).toHaveLength(6);
  });

  it("falls back to today's read when no model is matched", () => {
    expect(readDevicePanel("Example switch", swos, [], "all")).toEqual(fromDevice(swos, [], "all"));
    expect(readDevicePanel(undefined, swos, [], "all")).toEqual(fromDevice(swos, [], "all"));
    expect(readDevicePanel("SwOS CRS328-24P-4S+", swos, [], "all").groups).toHaveLength(4);
  });

  it("puts interfaces a preset doesn't name into an extra group", () => {
    const device = ["eth0", "eth1"].map((id, i) => port(id, i, "node"));
    const read = layoutOnPreset(preset("ubiquiti-udm-pro"), device, [], "all");
    expect(read.groups.at(-1)).toMatchObject({ kind: "rj45", count: 2 });
    expect(read.ports.filter((p) => p.interface).map((p) => p.interface)).toEqual(["eth0", "eth1"]);
  });
});

describe("sysDescr lookup", () => {
  it("reads sys_descr from the device's info series, and nothing when unreachable", async () => {
    const ok = {
      query: async () => ({
        series: [{ labels: { sys_descr: "SwOS CRS328-24P-4S+" }, values: [] }],
      }),
    };
    expect(await fetchSysDescr(ok as never, "id-1")).toBe("SwOS CRS328-24P-4S+");
    const down = {
      query: async () => {
        throw new Error("offline");
      },
    };
    expect(await fetchSysDescr(down as never, "id-1")).toBeUndefined();
  });
});
