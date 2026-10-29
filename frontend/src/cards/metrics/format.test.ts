import {
  formatBitsPerSecond,
  formatBytes,
  formatDuration,
  formatMs,
  formatPercent,
  formatRatio,
} from "./format";

describe("formatters", () => {
  it("formats bytes in binary units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(8 * 1024 ** 3)).toBe("8.0 GiB");
  });

  it("formats bit rates in decimal units", () => {
    expect(formatBitsPerSecond(2_500_000)).toBe("2.5 Mbit/s");
    expect(formatBitsPerSecond(0)).toBe("0 bit/s");
  });

  it("formats percentages, ratios and times", () => {
    expect(formatPercent(7.25)).toBe("7.3%");
    expect(formatPercent(42.4)).toBe("42%");
    expect(formatRatio(0.5)).toBe("50%");
    expect(formatMs(0.0012)).toBe("1.2 ms");
    expect(formatMs(0.34)).toBe("340 ms");
    expect(formatMs(1.5)).toBe("1.50 s");
  });

  it("formats durations with the two largest units", () => {
    expect(formatDuration(42)).toBe("42s");
    expect(formatDuration(5 * 3600 + 12 * 60 + 9)).toBe("5h 12m");
    expect(formatDuration(3 * 86400 + 4 * 3600)).toBe("3d 4h");
    expect(formatDuration(0)).toBe("0s");
  });

  it("shows a dash for missing or non-finite values", () => {
    expect(formatBytes(undefined)).toBe("–");
    expect(formatPercent(Number.NaN)).toBe("–");
    expect(formatDuration(-1)).toBe("–");
  });
});
