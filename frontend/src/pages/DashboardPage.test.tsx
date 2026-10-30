import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { CardPlacement, DashboardLayout } from "../api";
import { WIDGETS } from "../cards/widgets";
import { cardKey, prepareLayout } from "../cards/layout";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { exampleInstance, INSTANCE_ID } from "./devices/testFixtures";

beforeEach(() => {
  mockSystemTheme(false);
});

const SPEC = { card_type: "ping-check", title: "Ping", default_size: [4, 3] as [number, number] };
const PING: CardPlacement = {
  instance_id: INSTANCE_ID,
  card_type: "ping-check",
  x: 0,
  y: 0,
  w: 4,
  h: 3,
  settings: {},
};

function backendWith(layout: DashboardLayout, role: "admin" | "viewer" = "admin") {
  return fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /instances": () =>
        json([exampleInstance({ status: { ...exampleInstance().status, state: "down" } })]),
      [`GET /instances/${INSTANCE_ID}/cards`]: () => json([SPEC]),
      "GET /dashboards/1": () => json({ id: 1, name: "Default", layout }),
      "PUT /dashboards/1": (body) =>
        json({ id: 1, name: "Default", layout: (body as { layout: unknown }).layout }),
      "POST /query": () => json({ result_type: "vector", series: [], warnings: [] }),
    },
  });
}

const puts = (backend: ReturnType<typeof fakeBackend>) =>
  backend.requests().filter((r) => r.route === "PUT /dashboards/1");

function savedLayout(backend: ReturnType<typeof fakeBackend>): DashboardLayout {
  const call = backend.fetch.mock.calls.find(([, init]) => init?.method === "PUT");
  return (JSON.parse(String(call?.[1]?.body)) as { layout: DashboardLayout }).layout;
}

describe("dashboard page", () => {
  it("shows saved cards without adding the device's other cards or saving", async () => {
    const backend = backendWith({ version: 2, cards: [PING] });
    renderApp("/", backend);
    const card = await screen.findByTestId("card");
    expect(card).toHaveTextContent("Ping");
    expect(card).toHaveTextContent("nas-1");
    expect(screen.getAllByTestId("card")).toHaveLength(1);
    expect(puts(backend)).toHaveLength(0);
  });

  it("puts the dashboard-wide cards on an old layout, without saving it", async () => {
    const backend = backendWith({ cards: [PING] });
    renderApp("/", backend);
    expect(await screen.findByText("Devices healthy")).toBeInTheDocument();
    expect(screen.getAllByTestId("card")).toHaveLength(
      WIDGETS.filter((w) => w.card_type !== "network-map").length + 1,
    );
    expect(puts(backend)).toHaveLength(0);
  });

  it("offers remove buttons only in edit mode", async () => {
    const backend = backendWith({ version: 2, cards: [PING] });
    renderApp("/", backend);
    await screen.findByTestId("card");
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit dashboard" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Ping" }));
    expect(screen.queryAllByTestId("card")).toHaveLength(0);
    expect(puts(backend)).toHaveLength(0);
  });

  it("adds a card from the list and saves on Done", async () => {
    const backend = backendWith({ version: 2, cards: [] });
    renderApp("/", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Add your first card" }));
    const panel = screen.getByRole("region", { name: "Add a card" });
    fireEvent.click(within(panel).getByRole("button", { name: "+ Ping" }));
    fireEvent.click(within(panel).getByRole("button", { name: "+ Devices healthy" }));
    expect(screen.getAllByTestId("card")).toHaveLength(2);
    expect(within(panel).queryByRole("button", { name: "+ Ping" })).not.toBeInTheDocument();
    expect(puts(backend)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(puts(backend)).toHaveLength(1));
    const layout = savedLayout(backend);
    expect(layout.version).toBe(2);
    expect(layout.cards.map(cardKey)).toEqual([
      `${INSTANCE_ID}:ping-check`,
      "dashboard:devices-health",
    ]);
    expect(puts(backend)[0]?.csrf).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
  });

  it("Cancel throws the edits away", async () => {
    const backend = backendWith({ version: 2, cards: [PING] });
    renderApp("/", backend);
    await screen.findByTestId("card");
    fireEvent.click(screen.getByRole("button", { name: "Edit dashboard" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Ping" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getAllByTestId("card")).toHaveLength(1);
    expect(puts(backend)).toHaveLength(0);
  });

  it("offers no editing to a viewer", async () => {
    const backend = backendWith({ version: 2, cards: [PING] }, "viewer");
    renderApp("/", backend);
    expect(await screen.findByTestId("card")).toHaveTextContent("nas-1");
    expect(screen.queryByRole("button", { name: "Edit dashboard" })).not.toBeInTheDocument();
  });

  it("shows an error when the dashboard can't be loaded", async () => {
    const backend = fakeBackend({
      session: exampleMe(),
      routes: {
        "GET /instances": () => json([]),
        "GET /dashboards/1": () => json({ detail: "boom" }, 404),
      },
    });
    renderApp("/", backend);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});

describe("prepareLayout", () => {
  const placed: CardPlacement = {
    instance_id: "a",
    card_type: "t",
    x: 2,
    y: 0,
    w: 6,
    h: 2,
    settings: {},
  };
  const spec = (card_type: string) => ({
    card_type,
    title: card_type,
    default_size: [4, 3] as [number, number],
  });
  const widgets = [{ card_type: "w1", title: "W1", default_size: [3, 2] as [number, number] }];

  it("drops cards of deleted instances, but never prunes on a failed card request", () => {
    const stale = { ...placed, instance_id: "gone" };
    const other = { ...placed, instance_id: "b", card_type: "x" };
    const unknownWidget = { ...placed, instance_id: null, card_type: "nope" };
    const cards = prepareLayout(
      { version: 2, cards: [placed, stale, other, unknownWidget] },
      widgets,
      [{ instanceId: "a", spec: spec("t") }],
      new Set(["a", "b"]),
      new Set(["a"]),
    );
    expect(cards.map(cardKey)).toEqual(["a:t", "b:x"]);
  });

  it("adds no device cards, and puts widgets above an old layout once", () => {
    const available = [
      { instanceId: "a", spec: spec("t") },
      { instanceId: "a", spec: spec("u") },
    ];
    const current = prepareLayout(
      { version: 2, cards: [placed] },
      widgets,
      available,
      new Set(["a"]),
      new Set(["a"]),
    );
    expect(current).toEqual([placed]);

    const old = prepareLayout(
      { cards: [placed] },
      widgets,
      available,
      new Set(["a"]),
      new Set(["a"]),
    );
    expect(old.map((c) => [cardKey(c), c.x, c.y])).toEqual([
      ["dashboard:w1", 0, 0],
      ["a:t", 2, 2],
    ]);
  });
});
