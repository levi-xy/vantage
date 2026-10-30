import { isSfp, panelBlocks } from "./panel";

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
});
