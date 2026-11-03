import { faceplate, isSfp, panelBlocks } from "./panel";

describe("switch front panel", () => {
  it("lays 28 ports out as 8-8-8-4, odd ports on the bottom, even on top", () => {
    const ports = Array.from({ length: 28 }, (_, i) => i + 1);
    const blocks = panelBlocks(ports);
    expect(blocks.map((b) => b.length)).toEqual([8, 8, 8, 4]);
    const where = (n: number) => {
      const placed = blocks.flat().find((p) => p.port === n);
      return placed && [placed.column, placed.row];
    };
    expect(where(1)).toEqual([1, 2]); // bottom left
    expect(where(2)).toEqual([1, 1]); // above it
    expect(where(8)).toEqual([4, 1]);
    expect(where(9)).toEqual([1, 2]); // next block starts again
    expect(where(27)).toEqual([2, 2]); // the 4-port block is 2 columns wide
    expect(where(28)).toEqual([2, 1]);
  });

  it("tells SFP cages by name or a 10G speed", () => {
    expect(isSfp({ id: "sfpplus25", speed: 1e9 })).toBe(true);
    expect(isSfp({ id: "port25", speed: 10e9 })).toBe(true);
    expect(isSfp({ id: "port3", speed: 1e9 })).toBe(false);
  });

  it("puts the SFP cages in a separate block after the copper ports", () => {
    const ports = ["port1", "port2", "port3", "sfpplus1", "sfpplus2"].map((id) => ({ id }));
    const { copper, sfp } = faceplate(ports);
    expect(copper.flat().map((p) => p.port.id)).toEqual(["port1", "port2", "port3"]);
    expect(sfp.map((p) => [p.port.id, p.column, p.row])).toEqual([
      ["sfpplus1", 1, 2],
      ["sfpplus2", 1, 1],
    ]);
  });

  it("numbers SFP cages on their own by their id, not by ifIndex", () => {
    // A 24 + 4 switch whose cages come in ifIndex order 25-28 but not in name order.
    const ports = [
      { id: "ether1" },
      { id: "sfp-sfpplus2" },
      { id: "sfp-sfpplus1" },
      { id: "sfp-sfpplus4" },
      { id: "sfp-sfpplus3" },
    ];
    const { sfp } = faceplate(ports);
    expect(sfp.map((p) => [p.cage, p.port.id, p.column, p.row])).toEqual([
      [1, "sfp-sfpplus1", 1, 2], // 1 and 3 below
      [2, "sfp-sfpplus2", 1, 1], // 2 and 4 above
      [3, "sfp-sfpplus3", 2, 2],
      [4, "sfp-sfpplus4", 2, 1],
    ]);
  });

  it("keeps an unlinked SwOS cage in the SFP block, numbered 1-4", () => {
    // SwOS names every port port<N>; only port25 and port27 have a 10G link.
    const ports = Array.from({ length: 28 }, (_, i) => ({
      id: `port${i + 1}`,
      speed: i + 1 === 25 || i + 1 === 27 ? 10e9 : i < 24 ? 1e9 : undefined,
    }));
    const { copper, sfp } = faceplate(ports);
    expect(copper.flat()).toHaveLength(24);
    expect(sfp.map((p) => [p.cage, p.port.id, p.column, p.row])).toEqual([
      [1, "port25", 1, 2],
      [2, "port26", 1, 1],
      [3, "port27", 2, 2],
      [4, "port28", 2, 1],
    ]);
  });

  it("doesn't pull copper ports into the SFP block for a 10G port early on", () => {
    const ports = Array.from({ length: 20 }, (_, i) => ({
      id: `port${i + 1}`,
      speed: i === 0 ? 10e9 : 1e9,
    }));
    const { copper, sfp } = faceplate(ports);
    expect(sfp.map((p) => p.port.id)).toEqual(["port1"]);
    expect(copper.flat()).toHaveLength(19);
  });

  it("keeps bridges and copper listed after the cages out of the SFP block", () => {
    const after = [
      ...Array.from({ length: 24 }, (_, i) => ({ id: `ether${i + 1}` })),
      { id: "sfp-sfpplus1" },
      { id: "sfp-sfpplus2" },
      { id: "bridge1" },
      { id: "vlan10" },
    ];
    expect(faceplate(after).sfp.map((p) => p.port.id)).toEqual(["sfp-sfpplus1", "sfp-sfpplus2"]);
    const first = [
      { id: "sfp1" },
      ...Array.from({ length: 7 }, (_, i) => ({ id: `ether${i + 1}` })),
    ];
    const { copper, sfp } = faceplate(first);
    expect(sfp.map((p) => p.port.id)).toEqual(["sfp1"]);
    expect(copper.flat()).toHaveLength(7);
  });
});
