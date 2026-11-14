import { fireEvent, screen, within } from "@testing-library/react";
import { exampleMe, fakeBackend, json, mockSystemTheme, renderApp } from "../test-utils";
import type { AlertEvent, Instance } from "../api";
import type { ScrapeStatus } from "../cards/metrics/scrapeStatus";
import { exampleInstance } from "../pages/devices/testFixtures";
import { computeHealth } from "./health";
import type { HealthInput } from "./health";
import { REFRESH_MS } from "./live";

beforeEach(() => mockSystemTheme(false));

const at = Date.UTC(2026, 10, 2, 14, 15, 32);
const scrapeOk: ScrapeStatus = { states: new Map(), needed: 0, pending: false, failed: false };

const status = (state: Instance["status"]["state"]) => ({
  state,
  checked_at: null,
  last_success_at: null,
  error: state === "down" ? "no answer" : null,
});

const event = (overrides: Partial<AlertEvent>): AlertEvent => ({
  id: 1,
  rule_id: 1,
  instance_id: null,
  rule_name: "High CPU",
  severity: "warning",
  labels: {},
  fingerprint: "example",
  state: "firing",
  no_data_reason: null,
  started_at: "2026-11-02T13:00:00Z",
  state_since: null,
  ended_at: null,
  end_reason: null,
  summary: "x",
  deliveries: [],
  ...overrides,
});

function health(overrides: Partial<HealthInput> = {}) {
  return computeHealth({
    connection: "live",
    lastUpdate: at,
    now: at + REFRESH_MS / 2,
    instances: [exampleInstance({ status: status("up") })],
    instancesFailed: false,
    alerts: [],
    alertsFailed: false,
    scrape: scrapeOk,
    delivery: { stuck: 0, system_alerts: [] },
    deliveryFailed: false,
    ...overrides,
  });
}

describe("health level", () => {
  it("is healthy with nothing wrong, the ring filling over the refresh interval", () => {
    expect(health()).toEqual({ level: "healthy", issues: [], known: true, progress: 0.5 });
  });

  it("is critical for a down device or a critical alert, and critical beats warning", () => {
    expect(health({ instances: [exampleInstance({ status: status("down") })] }).level).toBe(
      "critical",
    );
    const mixed = health({
      alerts: [
        event({ id: 1, rule_name: "Ping loss" }),
        event({ id: 2, rule_name: "Disk full", severity: "critical" }),
      ],
      connection: "reconnecting",
    });
    expect(mixed.level).toBe("critical");
    expect(mixed.issues.map((i) => [i.level, i.title])).toEqual([
      ["critical", "Disk full"],
      ["warning", "Ping loss"],
      ["warning", "Live connection lost"],
    ]);
  });

  it("warns for other alerts, unknown devices, stale data, offline and system alerts", () => {
    const level = (o: Partial<HealthInput>) => health(o).level;
    expect(level({ alerts: [event({})] })).toBe("warning");
    expect(level({ alerts: [event({ state: "no_data", severity: "critical" })] })).toBe("warning");
    expect(
      level({
        instances: [exampleInstance({ type_id: "pfsense", status: status("unknown") })],
        scrape: { ...scrapeOk, needed: 1, failed: true },
      }),
    ).toBe("warning");
    expect(level({ now: at + 2 * REFRESH_MS })).toBe("healthy");
    const stale = health({ now: at + 2 * REFRESH_MS + 1 });
    expect(stale).toMatchObject({ level: "warning", progress: 1 });
    expect(stale.issues[0]).toMatchObject({ tag: "STALE", detail: "no update since 14:15:32" });
    expect(level({ connection: "reconnecting" })).toBe("warning");
    // The backend ended the socket (4401): no reconnect, so say so instead of "reconnecting".
    expect(health({ connection: "closed" }).issues[0]).toMatchObject({
      tag: "OFFLINE",
      title: "Live connection closed",
    });
    const lost = health({ connection: "reconnecting" }).issues[0];
    expect(lost?.title).toBe("Live connection lost");
    expect(lost?.detail).toContain("live updates are paused");
    expect(lost?.detail).toContain("reverse proxy");
    expect(level({ delivery: { stuck: 2 } })).toBe("warning");
    expect(
      level({
        delivery: {
          stuck: 0,
          system_alerts: [{ system_kind: "prometheus_unreachable", since: "2026-11-02T13:00:00Z" }],
        },
      }),
    ).toBe("warning");
    expect(
      level({
        delivery: {
          stuck: 0,
          system_alerts: [{ system_kind: "key_mismatch", since: "2026-11-02T13:00:00Z" }],
        },
      }),
    ).toBe("critical");
    expect(level({ alerts: undefined, alertsFailed: true })).toBe("warning");
    expect(level({ delivery: undefined, deliveryFailed: true })).toBe("warning");
  });

  it("counts a system alert once, as a warning, not as a critical firing alert", () => {
    const h = health({
      alerts: [
        event({
          source: "system",
          system_kind: "prometheus_unreachable",
          rule_id: null,
          rule_name: "Prometheus unreachable",
          severity: "critical",
          state: "firing",
        }),
      ],
      delivery: {
        stuck: 0,
        system_alerts: [{ system_kind: "prometheus_unreachable", since: "2026-11-02T13:00:00Z" }],
      },
    });
    expect(h.level).toBe("warning");
    expect(h.issues.map((i) => i.tag)).toEqual(["SYSTEM"]);
  });

  it("gives the cause of an unreachable Prometheus as the issue's detail", () => {
    const since = "2026-11-02T13:00:00Z";
    const issue = (reason: string | null) =>
      health({
        delivery: {
          stuck: 0,
          system_alerts: [{ system_kind: "prometheus_unreachable", since, reason }],
        },
      }).issues.find((i) => i.tag === "SYSTEM");
    expect(issue("name_not_found")?.detail).toMatch(/doesn't resolve/);
    expect(issue("connection_refused")?.detail).toMatch(/docker compose ps/);
    expect(issue(null)?.detail).toBe("");
    expect(issue("a_reason_from_a_newer_backend")?.detail).toBe("");
  });

  it("claims nothing until devices, alerts and delivery status have answered", () => {
    expect(health({ alerts: undefined }).known).toBe(false);
    expect(health({ delivery: undefined })).toMatchObject({ level: "healthy", known: false });
  });
});

describe("health status in the top bar", () => {
  it("lists each problem in a popover with links, and closes on Escape", async () => {
    const backend = fakeBackend({
      session: exampleMe("viewer"),
      routes: {
        "GET /instances": () =>
          json([exampleInstance({ name: "router-1", status: status("down") })]),
        "GET /alerts/events": () => json([event({ id: 2, rule_name: "Ping loss" })]),
      },
    });
    renderApp("/account", backend);
    const button = await screen.findByRole("button", { name: /^Health: Critical\b/ });
    expect(screen.getByText("Health: Critical")).toHaveAttribute("aria-live", "polite");
    fireEvent.click(button);
    const panel = screen.getByRole("dialog", { name: "System health" });
    expect(within(panel).getByText("DOWN")).toBeInTheDocument();
    // On desktop the panel sits above its button: its top edge must not be pinned.
    expect(panel).toHaveClass("md:top-auto", "md:bottom-0");
    expect(within(panel).getByRole("link", { name: "Open router-1 is down" })).toHaveAttribute(
      "href",
      "/devices",
    );
    expect(within(panel).getByRole("link", { name: "Open Ping loss" })).toHaveAttribute(
      "href",
      "/alerts",
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("says everything is healthy when nothing is wrong", async () => {
    renderApp("/account", fakeBackend({ session: exampleMe("viewer") }));
    fireEvent.click(await screen.findByRole("button", { name: /^Health: Healthy\b/ }));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      /^Everything is healthy · last update \d\d:\d\d:\d\d$/,
    );
  });
});
