import { QueryClient } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { applyMessage } from "../../app/live";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../../test-utils";
import type { FakeBackendOptions } from "../../test-utils";
import { exampleInstance, INSTANCE_ID } from "../devices/testFixtures";

beforeEach(() => {
  mockSystemTheme(false);
});

const CHANNEL = {
  id: 1,
  type: "telegram",
  name: "ops",
  config: { chat_id: "-1001234567890" },
  secrets_set: { bot_token: true },
};
const FIRING_EVENT = {
  id: 7,
  rule_id: 1,
  instance_id: null,
  rule_name: "Gateway down",
  severity: "critical",
  labels: { vantage_instance: "nas-1" },
  fingerprint: "f".repeat(32),
  state: "firing",
  started_at: "2026-10-30T10:00:00Z",
  ended_at: null,
  end_reason: null,
  summary: "nas-1 is down",
};
const TEMPLATE = {
  id: "instance-down-ping",
  name: "ping: instance down",
  description: "The instance has not answered for a few minutes.",
  integration_types: ["ping"],
  expr: 'vantage_instance_up{vantage_integration="ping"} == 0',
  for_seconds: 180,
  severity: "critical",
};
const RULE = {
  id: 3,
  name: "disk full",
  expr: "up == 0",
  for_seconds: 60,
  severity: "warning",
  instance_id: null,
  channel_ids: [1],
  enabled: true,
};

function adminBackend(routes: FakeBackendOptions["routes"] = {}) {
  return fakeBackend({
    session: exampleMe("admin"),
    routes: { "GET /channels": () => json([CHANNEL]), ...routes },
  });
}

function sentBody(backend: ReturnType<typeof fakeBackend>, route: string): unknown {
  const call = [...backend.fetch.mock.calls]
    .reverse()
    .find(
      ([input, init]) =>
        `${init?.method ?? "GET"} ${String(input).slice("/api/v1".length)}` === route,
    );
  if (!call) throw new Error(`${route} was not requested`);
  return call[1]?.body ? JSON.parse(String(call[1].body)) : undefined;
}

describe("alerts page: events and rules", () => {
  it("shows firing alerts and pages the history with before_id", async () => {
    const full = Array.from({ length: 50 }, (_, i) => ({
      ...FIRING_EVENT,
      id: 100 - i,
      state: "resolved",
      summary: `event ${100 - i}`,
    }));
    const older = full.map((e) => ({ ...e, id: e.id - 50, summary: `event ${e.id - 50}` }));
    const backend = adminBackend({
      // Open alerts: none. History: a full page, then older ids for before_id.
      "GET /alerts/events": (_body, url) =>
        json(url.includes("state=") ? [] : url.includes("before_id=") ? older.slice(0, 5) : full),
    });
    renderApp("/alerts", backend);
    const history = await screen.findByRole("list", { name: "Alert history" });
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await within(history).findByText("event 50")).toBeInTheDocument();
    expect(backend.requests().some((r) => r.route.includes("before_id=51"))).toBe(true);
  });

  it("pre-fills a rule from a template and shows the backend's refusal text", async () => {
    const backend = adminBackend({
      "GET /alerts/templates": () => json([TEMPLATE]),
      "POST /alerts/rules": () => json({ detail: "expr refused: parse error" }, 422),
    });
    renderApp("/alerts", backend);
    const toggle = await screen.findByRole("button", { name: "Create from template · 1" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("list", { name: "Templates" })).toBeNull();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Use template ping: instance down" }));
    const form = await screen.findByRole("form", { name: "Add a rule" });
    expect(within(form).getByLabelText<HTMLInputElement>("Rule name").value).toBe(TEMPLATE.name);
    fireEvent.click(await within(form).findByRole("checkbox", { name: "ops" }));
    fireEvent.click(within(form).getByRole("button", { name: "Add rule" }));
    expect(await within(form).findByText(/expr refused: parse error/)).toBeVisible();
    expect(sentBody(backend, "POST /alerts/rules")).toMatchObject({
      expr: TEMPLATE.expr,
      for_seconds: 180,
      severity: "critical",
      channel_ids: [1],
      instance_id: null,
    });
  });

  it("disables a rule with a PATCH", async () => {
    const backend = adminBackend({
      "GET /alerts/rules": () => json([RULE]),
      "PATCH /alerts/rules/3": () => json({ ...RULE, enabled: false }),
    });
    renderApp("/alerts", backend);
    fireEvent.click(await screen.findByRole("button", { name: "Disable disk full" }));
    await waitFor(() =>
      expect(sentBody(backend, "PATCH /alerts/rules/3")).toEqual({ enabled: false }),
    );
  });

  it("lets a viewer see events but never requests rules, templates or channels", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: { "GET /alerts/events": () => json([FIRING_EVENT]) },
    });
    renderApp("/alerts", backend);
    const firing = await screen.findByRole("list", { name: "Open alerts" });
    expect(within(firing).getByText("nas-1 is down")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Rules" })).toBeNull();
    const routes = backend.requests().map((r) => r.route);
    expect(routes.some((r) => /alerts\/rules|templates|channels/.test(r))).toBe(false);
  });

  it("names the device, links to it and lets an admin close an open event", async () => {
    const linked = { ...FIRING_EVENT, instance_id: INSTANCE_ID, labels: {} };
    const closed = {
      ...FIRING_EVENT,
      id: 6,
      state: "resolved",
      labels: {},
      ended_at: "2026-10-30T11:00:00Z",
      end_reason: "rule_changed",
    };
    const backend = adminBackend({
      "GET /instances": () => json([exampleInstance({ name: "fw-a" })]),
      "GET /alerts/events": () => json([linked, closed]),
      "POST /alerts/events/7/close": () => json({ ...linked, state: "resolved" }),
    });
    renderApp("/alerts", backend);
    const firing = await screen.findByRole("list", { name: "Open alerts" });
    expect(await within(firing).findByRole("link", { name: "fw-a" })).toHaveAttribute(
      "href",
      `/devices/${INSTANCE_ID}`,
    );
    expect(within(firing).getAllByText("Gateway down").length).toBeGreaterThan(0);
    const history = screen.getByRole("list", { name: "Alert history" });
    expect(within(history).getByText("—")).toBeInTheDocument();
    expect(within(history).getByText(/rule changed/)).toBeInTheDocument();
    expect(within(history).queryByRole("button", { name: /Close alert/ })).toBeNull();

    fireEvent.click(within(firing).getByRole("button", { name: "Close alert 7" }));
    expect(backend.requests().some((r) => r.route.includes("/close"))).toBe(false);
    fireEvent.click(within(firing).getByRole("button", { name: "Confirm closing alert 7" }));
    await waitFor(() =>
      expect(backend.requests().some((r) => r.route === "POST /alerts/events/7/close")).toBe(true),
    );
  });

  it("shows no Close button to a viewer", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: { "GET /alerts/events": () => json([FIRING_EVENT]) },
    });
    renderApp("/alerts", backend);
    const firing = await screen.findByRole("list", { name: "Open alerts" });
    expect(within(firing).getByText("nas-1")).toBeInTheDocument();
    expect(within(firing).queryByRole("button", { name: /Close alert/ })).toBeNull();
  });

  it("shows each event's notifications compactly", async () => {
    const delivery = {
      channel_id: 1,
      channel_name: "ops",
      kind: "firing",
      status: "pending",
      attempts: 3,
      error_kind: "rate_limited",
      created_at: "2026-10-30T10:00:00Z",
      finished_at: null,
    };
    const event = {
      ...FIRING_EVENT,
      deliveries: [
        delivery,
        {
          ...delivery,
          channel_id: null,
          channel_name: null,
          status: "cancelled",
          attempts: 0,
          error_kind: null,
        },
        { ...delivery, kind: "resolved", status: "folded", attempts: 0, error_kind: null },
        {
          ...delivery,
          kind: "resolved",
          status: "folded",
          attempts: 1,
          error_kind: null,
          finished_at: "2026-10-30T10:05:00Z",
        },
      ],
    };
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: { "GET /alerts/events": () => json([event]) },
    });
    renderApp("/alerts", backend);
    const firing = await screen.findByRole("list", { name: "Open alerts" });
    // Collapsed to a count until asked for.
    expect(within(firing).queryByRole("list", { name: "Notifications" })).toBeNull();
    const toggle = within(firing).getByRole("button", { name: "4 notifications · 2 waiting" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const lines = within(firing).getByRole("list", { name: "Notifications" });
    expect(
      within(lines).getByText("firing to ops: queued (3 attempts, rate limited)"),
    ).toBeVisible();
    expect(
      within(lines).getByText(
        "firing to deleted channel: cancelled (channel deleted) (0 attempts)",
      ),
    ).toBeVisible();
    // Waiting in an overflow summary is not shown as sent until the summary is.
    expect(within(lines).getByText("resolved to ops: queued in summary")).toBeVisible();
    expect(
      within(lines).getByText("resolved to ops: sent in a combined message (1 attempt)"),
    ).toBeVisible();
  });

  it("shows a banner while notifications are stuck", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: { "GET /alerts/delivery-status": () => json({ stuck: 2 }) },
    });
    renderApp("/", backend);
    const banner = await screen.findByText(/2 alert notifications have failed or waited/);
    expect(banner).toBeVisible();
    expect(within(banner).getByRole("link", { name: "See Alerts" })).toHaveAttribute(
      "href",
      "/alerts",
    );
  });

  it("shows a banner while a system alert fires", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: {
        "GET /alerts/delivery-status": () =>
          json({
            stuck: 0,
            system_alerts: [
              { system_kind: "prometheus_unreachable", since: "2026-10-30T10:00:00Z" },
            ],
          }),
      },
    });
    renderApp("/", backend);
    const banner = await screen.findByText(/Prometheus is unreachable/);
    expect(banner).toBeVisible();
    expect(screen.queryByText(/alert notifications have failed/)).toBeNull();
  });

  it("says why Prometheus is unreachable and how to fix it", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: {
        "GET /alerts/delivery-status": () =>
          json({
            stuck: 0,
            system_alerts: [
              {
                system_kind: "prometheus_unreachable",
                since: "2026-10-30T10:00:00Z",
                reason: "address_refused",
              },
            ],
          }),
      },
    });
    renderApp("/", backend);
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Prometheus is unreachable");
    expect(banner).toHaveTextContent("The network guard refuses its address");
    expect(banner).toHaveTextContent("VANTAGE_PROMETHEUS_URL");
  });

  it("shows a NO DATA alert with its reason in plain words, closable by an admin", async () => {
    const noData = {
      ...FIRING_EVENT,
      state: "no_data",
      no_data_reason: "source_down",
      state_since: "2026-10-30T10:05:00Z",
      deliveries: [
        {
          channel_id: 1,
          channel_name: "ops",
          kind: "no_data",
          status: "delivered",
          attempts: 1,
          error_kind: null,
          created_at: "2026-10-30T10:10:00Z",
          finished_at: "2026-10-30T10:10:01Z",
        },
      ],
    };
    const backend = adminBackend({
      "GET /alerts/events": () => json([noData]),
      "POST /alerts/events/7/close": () => json({ ...noData, state: "resolved" }),
    });
    renderApp("/alerts", backend);
    const open = await screen.findByRole("list", { name: "Open alerts" });
    expect(within(open).getByText("No data: device not answering")).toBeVisible();
    expect(within(open).getByText(/no data since/)).toBeVisible();
    fireEvent.click(within(open).getByRole("button", { name: "1 notification" }));
    expect(within(open).getByText("no data to ops: delivered (1 attempt)")).toBeVisible();
    fireEvent.click(within(open).getByRole("button", { name: "Close alert 7" }));
    fireEvent.click(within(open).getByRole("button", { name: "Confirm closing alert 7" }));
    await waitFor(() =>
      expect(backend.requests().some((r) => r.route === "POST /alerts/events/7/close")).toBe(true),
    );
    // Both open states are asked for.
    const urls = backend.fetch.mock.calls.map(([input]) => String(input));
    expect(urls.some((u) => u.includes("state=no_data"))).toBe(true);
    expect(urls.some((u) => u.includes("state=firing"))).toBe(true);
  });

  it("marks a system alert, shows the series' labels and offers no Close", async () => {
    const system = {
      ...FIRING_EVENT,
      id: 8,
      rule_id: null,
      rule_name: "Prometheus unreachable",
      labels: {},
      source: "system",
      system_kind: "prometheus_unreachable",
    };
    const labelled = { ...FIRING_EVENT, labels: { vantage_instance: "nas-1", ifname: "eth0" } };
    const backend = adminBackend({ "GET /alerts/events": () => json([system, labelled]) });
    renderApp("/alerts", backend);
    const open = await screen.findByRole("list", { name: "Open alerts" });
    expect(within(open).getByText("system alert, closes by itself")).toBeVisible();
    expect(within(open).queryByRole("button", { name: "Close alert 8" })).toBeNull();
    expect(within(open).getByRole("button", { name: "Close alert 7" })).toBeVisible();
    const labels = within(open).getByRole("list", { name: "Labels" });
    expect(within(labels).getByText("ifname=eth0")).toBeVisible();
    // The device is named in the row, not repeated as a label.
    expect(within(labels).queryByText(/vantage_instance/)).toBeNull();
  });

  it("warns about rules that resolve on an empty result and shows the last error", async () => {
    const backend = adminBackend({
      "GET /alerts/rules": () =>
        json([{ ...RULE, recovery_mode: "empty_result", error: "query timed out" }]),
    });
    renderApp("/alerts", backend);
    expect(await screen.findByText(/Resolves when the result is empty/)).toBeVisible();
    expect(screen.getByText("Last query error: query timed out")).toBeVisible();
  });

  it("refetches events when an alert_event is pushed", () => {
    const queryClient = new QueryClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    applyMessage(queryClient, JSON.stringify({ type: "alert_event", event_id: 1 }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["alerts"] });
  });
});
