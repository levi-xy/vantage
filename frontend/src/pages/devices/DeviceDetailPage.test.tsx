import { screen, within } from "@testing-library/react";
import type { AlertEvent } from "../../api";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../../test-utils";
import { exampleInstance, INSTANCE_ID } from "./testFixtures";

beforeEach(() => {
  mockSystemTheme(false);
});

const OTHER_ID = "00000000-0000-4000-8000-000000000002";

function event(id: number, instanceId: string | null, state: "firing" | "no_data"): AlertEvent {
  return {
    id,
    rule_id: 1,
    instance_id: instanceId,
    rule_name: `Rule ${id}`,
    severity: "critical",
    labels: {},
    fingerprint: `fp-${id}`,
    state,
    started_at: "2026-11-01T10:00:00Z",
    ended_at: null,
    end_reason: null,
    summary: "",
    deliveries: [],
  };
}

function backendFor(role: "admin" | "viewer", events: AlertEvent[] = []) {
  return fakeBackend({
    session: exampleMe(role),
    routes: {
      "GET /instances": () => json([exampleInstance()]),
      [`GET /instances/${INSTANCE_ID}`]: () =>
        json(
          exampleInstance({
            status: {
              state: "down",
              checked_at: null,
              last_success_at: null,
              error: "connection refused by 10.0.0.10",
            },
          }),
        ),
      [`GET /instances/${INSTANCE_ID}/cards`]: () =>
        json([
          { card_type: "ping-check", title: "Ping", default_size: [3, 3] },
          { card_type: "no-such-card", title: "Mystery", default_size: [3, 3] },
        ]),
      "GET /alerts/events": (_body, url) =>
        json(events.filter((e) => String(url).includes(`state=${e.state}`))),
      "POST /query": () => json({ result_type: "vector", series: [], warnings: [] }),
    },
  });
}

describe("device detail page", () => {
  it("shows the device, only its open alerts and its cards, with an Edit link for admins", async () => {
    renderApp(
      `/devices/${INSTANCE_ID}`,
      backendFor("admin", [event(1, INSTANCE_ID, "firing"), event(2, OTHER_ID, "firing")]),
    );
    expect(await screen.findByRole("heading", { level: 1, name: /nas-1/ })).toBeInTheDocument();
    expect(screen.getByText("connection refused by 10.0.0.10")).toBeInTheDocument();
    expect(screen.getByText(/lab/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute(
      "href",
      `/devices/${INSTANCE_ID}/edit`,
    );
    const alerts = await screen.findByRole("region", { name: "Open alerts" });
    expect(await within(alerts).findByText("Rule 1")).toBeInTheDocument();
    expect(within(alerts).queryByText("Rule 2")).toBeNull();
    expect((await screen.findAllByTestId("detail-card")).length).toBeGreaterThan(0);
  });

  it("is read-only for a viewer and says when there are no open alerts", async () => {
    renderApp(`/devices/${INSTANCE_ID}`, backendFor("viewer"));
    expect(await screen.findByText("No open alerts")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
  });

  it("reports an unknown device", async () => {
    renderApp(
      `/devices/${OTHER_ID}`,
      fakeBackend({
        session: exampleMe("viewer"),
        routes: {
          [`GET /instances/${OTHER_ID}`]: () => json({ detail: "instance not found" }, 404),
        },
      }),
    );
    expect(await screen.findByRole("heading", { name: "Device not found" })).toBeInTheDocument();
  });
});
