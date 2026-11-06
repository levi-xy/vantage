import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import { AreaChart } from "./Charts";

const T0 = 1_800_000_000;

function chart(second: [number, number][]) {
  return render(
    <AreaChart
      label="Traffic"
      format={(v) => `${v} bit/s`}
      series={[
        {
          name: "Download",
          tone: "in",
          points: [
            [T0, 10],
            [T0 + 60, 20],
            [T0 + 120, 30],
          ],
        },
        { name: "Upload", tone: "out", points: second },
      ]}
    />,
  );
}

function hoverAt(frac: number) {
  const svg = screen.getByRole("img", { name: "Traffic" });
  svg.getBoundingClientRect = () => ({ left: 0, width: 200 }) as DOMRect;
  // jsdom has no PointerEvent, so clientX has to be set by hand.
  const event = createEvent.pointerMove(svg);
  Object.defineProperty(event, "clientX", { value: frac * 200 });
  fireEvent(svg, event);
  return svg;
}

describe("AreaChart hover", () => {
  it("shows each series' nearest value, and hides on leave", () => {
    chart([
      [T0, 1],
      [T0 + 60, 2],
      [T0 + 120, 3],
    ]);
    expect(screen.queryByRole("status")).toBeNull();
    const svg = hoverAt(0.5);
    const tip = screen.getByRole("status");
    expect(tip).toHaveTextContent("Download20 bit/s");
    expect(tip).toHaveTextContent("Upload2 bit/s");
    fireEvent.pointerLeave(svg);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("says no data where a series has a gap", () => {
    chart([
      [T0, 1],
      [T0 + 60, 2],
      [T0 + 600, 3],
    ]);
    // Hovering near the end is far from Download's last sample: a gap, not a stale value.
    hoverAt(0.8);
    expect(screen.getByRole("status")).toHaveTextContent("Downloadno data");
  });
});
