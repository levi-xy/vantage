import { QueryClient } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { applyMessage } from "../../app/live";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../../test-utils";
import type { FakeBackendOptions } from "../../test-utils";

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
  state: "firing",
  started_at: "2026-10-30T10:00:00Z",
  ended_at: null,
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
    const backend = adminBackend({ "GET /alerts/events": () => json(full) });
    renderApp("/alerts", backend);
    expect(await screen.findByRole("list", { name: "Firing alerts" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    await waitFor(() =>
      expect(backend.requests().some((r) => r.route.includes("before_id=51"))).toBe(true),
    );
  });

  it("pre-fills a rule from a template and shows the backend's refusal text", async () => {
    const backend = adminBackend({
      "GET /alerts/templates": () => json([TEMPLATE]),
      "POST /alerts/rules": () => json({ detail: "expr refused: parse error" }, 422),
    });
    renderApp("/alerts", backend);
    fireEvent.click(
      await screen.findByRole("button", { name: "Use template ping: instance down" }),
    );
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
    const firing = await screen.findByRole("list", { name: "Firing alerts" });
    expect(within(firing).getByText("nas-1 is down")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Rules" })).toBeNull();
    const routes = backend.requests().map((r) => r.route);
    expect(routes.some((r) => /alerts\/rules|templates|channels/.test(r))).toBe(false);
  });

  it("refetches events when an alert_event is pushed", () => {
    const queryClient = new QueryClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    applyMessage(queryClient, JSON.stringify({ type: "alert_event", event_id: 1 }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["alerts"] });
  });
});
