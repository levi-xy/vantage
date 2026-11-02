import { areaPath, hourTicks, linePath, niceTicks, stats } from "./chart";

const box = { start: 0, end: 100, min: 0, max: 10, width: 1000, height: 100 };

describe("chart paths", () => {
  it("maps points into the box, top is the max", () => {
    expect(
      linePath(
        [
          [0, 0],
          [50, 5],
          [100, 10],
        ],
        box,
      ),
    ).toBe("M0.0 100.0 L500.0 50.0 L1000.0 0.0");
    expect(linePath([[0, 1]], box)).toBe("");
  });

  it("closes an area down to the bottom edge", () => {
    expect(
      areaPath(
        [
          [0, 10],
          [100, 10],
        ],
        box,
      ),
    ).toBe("M0.0 0.0 L1000.0 0.0 L1000.0 100.0 L0.0 100.0 Z");
  });

  it("picks round gridlines at or above the max", () => {
    expect(niceTicks(84e6)).toEqual([0, 25e6, 50e6, 75e6, 100e6]);
    expect(niceTicks(12)).toEqual([0, 5, 10, 15]);
    expect(niceTicks(0)).toEqual([0, 1]);
  });

  it("labels full hours inside the window, away from the edges", () => {
    const start = new Date(2026, 10, 2, 8, 30).getTime() / 1000;
    const ticks = hourTicks(start, start + 6 * 3600);
    expect(ticks.map((t) => new Date(t * 1000).getHours())).toEqual([9, 10, 11, 12, 13, 14]);
  });

  it("gives min, average and max", () => {
    expect(
      stats([
        [0, 1],
        [1, 2],
        [2, 6],
      ]),
    ).toEqual({ min: 1, avg: 3, max: 6 });
    expect(stats([])).toBeUndefined();
  });
});
