import { screen, waitFor } from "@testing-library/react";
import type { CardPlacement } from "../api";
import { cardKey, mergeLayout } from "../cards/layout";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import { exampleInstance, INSTANCE_ID } from "./devices/testFixtures";

beforeEach(() => {
  mockSystemTheme(false);
});

const SPEC = { card_type: "ping-check", title: "Ping", default_size: [4, 3] as [number, number] };

function backendWith(cards: CardPlacement[], role: "admin" | "viewer" = "admin") {
  return fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /instances": () =>
        json([exampleInstance({ status: { ...exampleInstance().status, state: "down" } })]),
      [`GET /instances/${INSTANCE_ID}/cards`]: () => json([SPEC]),
      "GET /dashboards/1": () => json({ id: 1, name: "Default", layout: { cards } }),
      "PUT /dashboards/1": (body) =>
        json({ id: 1, name: "Default", layout: (body as { layout: unknown }).layout }),
    },
  });
}

const puts = (backend: ReturnType<typeof fakeBackend>) =>
  backend.requests().filter((r) => r.route === "PUT /dashboards/1");

describe("dashboard page", () => {
  it("adds a new instance's card as a placeholder and saves it (admin)", async () => {
    const backend = backendWith([]);
    renderApp("/", backend);
    const card = await screen.findByTestId("card");
    expect(card).toHaveTextContent("Ping");
    expect(card).toHaveTextContent("nas-1");
    expect(card).toHaveTextContent("Down");
    expect(screen.queryByText(/No cards yet/)).not.toBeInTheDocument();

    await waitFor(() => expect(puts(backend)).toHaveLength(1));
    const call = backend.fetch.mock.calls.find(([, init]) => init?.method === "PUT");
    const body = JSON.parse(String(call?.[1]?.body)) as { layout: { cards: CardPlacement[] } };
    expect(body.layout.cards).toEqual([
      { instance_id: INSTANCE_ID, card_type: "ping-check", x: 0, y: 0, w: 4, h: 3, settings: {} },
    ]);
    expect(puts(backend)[0]?.csrf).toBeTruthy();
  });

  it("shows the cards to a viewer without saving anything", async () => {
    const backend = backendWith([], "viewer");
    renderApp("/", backend);
    expect(await screen.findByTestId("card")).toHaveTextContent("nas-1");
    expect(puts(backend)).toHaveLength(0);
  });

  it("shows the empty state when there are no devices", async () => {
    const backend = fakeBackend({
      session: exampleMe(),
      routes: {
        "GET /instances": () => json([]),
        "GET /dashboards/1": () => json({ id: 1, name: "Default", layout: { cards: [] } }),
      },
    });
    renderApp("/", backend);
    expect(await screen.findByText(/No cards yet/)).toBeInTheDocument();
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

describe("mergeLayout", () => {
  const placed: CardPlacement = {
    instance_id: "a",
    card_type: "t",
    x: 2,
    y: 0,
    w: 6,
    h: 2,
    settings: {},
  };
  const spec = (card_type: string, w = 4, h = 3) => ({
    card_type,
    title: card_type,
    default_size: [w, h] as [number, number],
  });

  it("keeps saved positions and flows new cards below, wrapping at 12 columns", () => {
    const available = [
      { instanceId: "a", spec: spec("t") },
      { instanceId: "b", spec: spec("u", 8) },
      { instanceId: "b", spec: spec("v", 8) },
    ];
    const { cards, changed } = mergeLayout(
      [placed],
      available,
      new Set(["a", "b"]),
      new Set(["a", "b"]),
    );
    expect(changed).toBe(true);
    expect(cards[0]).toEqual(placed);
    expect(cards.slice(1).map((c) => [c.x, c.y])).toEqual([
      [0, 2],
      [0, 5],
    ]);
  });

  it("drops cards of deleted instances, but never prunes on a failed card request", () => {
    const stale = { ...placed, instance_id: "gone" };
    const other = { ...placed, instance_id: "b", card_type: "x" };
    const result = mergeLayout(
      [placed, stale, other],
      [{ instanceId: "a", spec: spec("t") }],
      new Set(["a", "b"]),
      new Set(["a"]),
    );
    expect(result.cards.map(cardKey)).toEqual(["a:t", "b:x"]);
    expect(result.changed).toBe(true);
  });

  it("reports no change when the layout is complete", () => {
    const { changed } = mergeLayout(
      [placed],
      [{ instanceId: "a", spec: spec("t") }],
      new Set(["a"]),
      new Set(["a"]),
    );
    expect(changed).toBe(false);
  });
});
