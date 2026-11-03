import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { ApiClient } from "../api";
import type { CardPlacement, Instance } from "../api";
import { DEVICE_COLORS, deviceColor } from "../app/deviceColors";
import { DashboardGrid } from "./DashboardGrid";
import { KeyFigure } from "./metrics/KeyFigure";
import { StatTile } from "./metrics/StatTile";
import { PingCheckCard } from "./ping-check/PingCheckCard";
import { renderCard, testInstance, vector } from "./testRender";

const client = new ApiClient({ fetch: vi.fn() });
vi.mock("../app/session", () => ({ useApiClient: () => client }));

const device: Instance = { ...testInstance, enabled: true, color: "violet" };

describe("deviceColor", () => {
  it("maps a palette name to its theme token, and anything else to no colour", () => {
    expect(deviceColor({ color: "violet" })).toBe("var(--vt-dev-violet)");
    expect(deviceColor({ color: null })).toBeUndefined();
    expect(deviceColor({ color: "red" })).toBeUndefined();
    expect(deviceColor(undefined)).toBeUndefined();
    // Same names and order as the backend palette.
    expect(DEVICE_COLORS.map((c) => c.name)).toEqual([
      ...["blue", "violet", "teal", "pink", "orange", "lime"],
      ...["cyan", "indigo", "fuchsia", "sky", "purple"],
    ]);
  });
});

describe("a device card", () => {
  it("tints the header chip with the device colour and shows UP in green", async () => {
    renderCard(client, PingCheckCard, (q) => vector([[{}, q.includes("rtt") ? "0.01" : "0"]]), {
      ...device,
      type_id: "ping_check",
    });
    const state = await screen.findByText("UP");
    expect(state).toHaveClass("text-ok-text", "font-semibold");
    const chip = screen.getByTestId("type-icon");
    expect(chip).toHaveStyle({ color: "var(--vt-dev-violet)" });
    expect(chip.style.backgroundColor).toContain("var(--vt-dev-violet) 15%");
    // The main figure is in the device colour.
    expect(await screen.findByText("10.0 ms")).toHaveClass("text-device");
  });

  it("keeps the problem colours on figures; the chip keeps the device colour", async () => {
    renderCard(client, PingCheckCard, (q) => vector([[{}, q.includes("rtt") ? "0.01" : "1"]]), {
      ...device,
      type_id: "ping_check",
    });
    const state = await screen.findByText("100% LOSS");
    expect(state).toHaveClass("text-crit-text");
    expect(screen.getByTestId("type-icon")).toHaveStyle({ color: "var(--vt-dev-violet)" });
  });

  it("figures: a problem tone wins over the device colour", () => {
    render(
      <>
        <StatTile label="Plain" value="1" />
        <StatTile label="Bad" value="2" tone="bad" />
        <StatTile label="Good" value="3" tone="ok" />
        <KeyFigure label="Line">4</KeyFigure>
        <KeyFigure label="Warn" tone="warn">
          5
        </KeyFigure>
      </>,
    );
    expect(screen.getByText("1")).toHaveClass("text-device");
    expect(screen.getByText("2")).toHaveClass("text-crit-text");
    expect(screen.getByText("2")).not.toHaveClass("text-device");
    expect(screen.getByText("3")).toHaveClass("text-ok-text");
    expect(screen.getByRole("group", { name: "Line" })).toHaveClass("text-device");
    expect(screen.getByRole("group", { name: "Warn" })).toHaveClass("text-warn");
    expect(screen.getByRole("group", { name: "Warn" })).not.toHaveClass("text-device");
  });
});

describe("the dashboard grid", () => {
  it("gives every card of a device its colour strip, and widgets none", () => {
    const place = (instance_id: string | null, card_type: string, y: number): CardPlacement => ({
      instance_id,
      card_type,
      x: 0,
      y,
      w: 3,
      h: 2,
      settings: {},
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DashboardGrid
          compact
          cards={[
            place(device.id, "not-built-a", 0),
            place(device.id, "not-built-b", 1),
            place(null, "not-a-widget", 2),
          ]}
          instances={new Map([[device.id, device]])}
          titles={new Map()}
          editing={false}
          onChange={() => {}}
          onRemove={() => {}}
          onConfigure={() => {}}
        />
      </QueryClientProvider>,
    );
    const [a, b, widget] = screen.getAllByTestId("card") as HTMLElement[];
    for (const card of [a, b] as HTMLElement[]) {
      expect(card.style.getPropertyValue("--vt-device")).toBe("var(--vt-dev-violet)");
      expect(within(card).getByTestId("device-strip")).toHaveClass("border-device");
    }
    expect(widget?.style.getPropertyValue("--vt-device")).toBe("");
    expect(within(widget as HTMLElement).queryByTestId("device-strip")).toBeNull();
  });
});
