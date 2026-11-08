import { afterEach } from "vitest";
import { formatClock, formatClockOrDay, formatDateTime, setTimeZone } from "./time";

afterEach(() => setTimeZone("UTC"));

const AT = Date.UTC(2026, 10, 7, 13, 5, 3);

describe("time formatting", () => {
  it("uses the configured zone, not the browser's", () => {
    expect(formatDateTime(AT)).toBe("2026-11-07 13:05:03");
    setTimeZone("Europe/Berlin");
    expect(formatDateTime(AT)).toBe("2026-11-07 14:05:03");
    expect(formatClock(AT)).toBe("14:05");
    setTimeZone("Asia/Kolkata");
    expect(formatClock(AT)).toBe("18:35");
  });

  it("decides 'today' in the configured zone and falls back to UTC for a bad name", () => {
    setTimeZone("Pacific/Auckland");
    // 13:05 UTC on the 7th is 02:05 on the 8th in Auckland.
    expect(formatClockOrDay(AT, Date.UTC(2026, 10, 7, 11, 0))).toBe("02:05");
    expect(formatClockOrDay(AT, Date.UTC(2026, 10, 7, 10, 0))).toBe("8 Nov 02:05");
    setTimeZone("Not/AZone");
    expect(formatClock(AT)).toBe("13:05");
  });
});
