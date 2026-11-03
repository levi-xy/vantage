// The top bar's health status: one level (Healthy / Warning / Critical) and the issues
// behind it. Pure functions, so they're easy to test.

import type { AlertEvent, DeliveryStatusSummary, Instance } from "../api";
import { shownState } from "../cards/metrics/scrapeStatus";
import type { ScrapeStatus } from "../cards/metrics/scrapeStatus";
import { findProblems } from "../cards/problems";
import { clockTime, REFRESH_MS } from "./live";
import type { LiveState } from "./live";

export type HealthLevel = "healthy" | "warning" | "critical";

export interface HealthIssue {
  key: string;
  level: "warning" | "critical";
  /** A short upper-case tag: DOWN, FIRING, NO DATA, UNKNOWN, STALE, OFFLINE, ... */
  tag: string;
  title: string;
  /** Short and secret-free; "" when there is nothing to add. */
  detail: string;
  /** Epoch milliseconds, or null when unknown. */
  since: number | null;
  /** Where "Open" goes, or null when there is nowhere to go. */
  href: string | null;
}

export interface Health {
  level: HealthLevel;
  /** Critical ones first, then warnings. */
  issues: HealthIssue[];
  /** False until devices, alerts and delivery status have answered once (or failed). */
  known: boolean;
  /** How far the refresh ring is filled, 0 to 1. */
  progress: number;
}

export interface HealthInput {
  connection: LiveState;
  /** When data last arrived (epoch ms), or null before the first answer. */
  lastUpdate: number | null;
  now: number;
  /** undefined until the list has answered. */
  instances: Instance[] | undefined;
  instancesFailed: boolean;
  alerts: AlertEvent[] | undefined;
  alertsFailed: boolean;
  scrape: ScrapeStatus;
  delivery: DeliveryStatusSummary | undefined;
  deliveryFailed: boolean;
  intervalMs?: number;
}

const SYSTEM_ALERT_TEXT: Record<string, string> = {
  prometheus_unreachable: "Prometheus is unreachable: alert rules can't be checked.",
  metrics_not_collected:
    "Prometheus isn't collecting Vantage's metrics: polled devices can't be checked.",
  evaluator_stalled: "Alert evaluation has stopped.",
  key_mismatch:
    "The master key doesn't match the stored secrets: polling and notifications are stopped.",
};

/** System alerts that stop monitoring altogether. */
const CRITICAL_SYSTEM_ALERTS = new Set(["key_mismatch"]);

export function systemAlertText(kind: string): string {
  return SYSTEM_ALERT_TEXT[kind] ?? `System alert: ${kind.replace(/_/g, " ")}.`;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function parseTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/**
 * Critical: a device is down or an alert of severity critical fires. Warning: any other
 * problem (other firing alerts, no-data alerts), devices nobody could judge, data older than
 * two refresh intervals, the live connection lost, a system alert or stuck notifications.
 */
export function computeHealth(input: HealthInput): Health {
  const { connection, lastUpdate, now, scrape } = input;
  const intervalMs = input.intervalMs ?? REFRESH_MS;
  const list = input.instances ?? [];
  const stateOf = (i: Instance) => shownState(i, scrape.states);
  const issues: HealthIssue[] = [];

  for (const p of findProblems(list, input.alerts ?? [], stateOf)) {
    issues.push({ ...p, level: p.critical ? "critical" : "warning" });
  }

  if (input.instances !== undefined && !scrape.pending) {
    // Enabled devices nobody could judge: scrape-only ones while Prometheus fails, or ones
    // Prometheus has no result for yet. They are never counted as up.
    const unknown = list.filter((i) => i.enabled && stateOf(i) === "unknown").length;
    if (unknown) {
      issues.push({
        key: "unknown",
        level: "warning",
        tag: "UNKNOWN",
        title: `Status unknown for ${plural(unknown, "device")}`,
        detail: scrape.failed ? "Prometheus can't be reached" : "no scrape result yet",
        since: null,
        href: "/devices",
      });
    }
  }
  if (input.instancesFailed) {
    issues.push(unavailable("instances", "Device list unavailable", "/devices"));
  }
  if (input.alertsFailed) {
    issues.push(unavailable("alerts", "Alert list unavailable", "/alerts"));
  }
  if (input.deliveryFailed) {
    issues.push(unavailable("delivery-status", "Notification status unavailable", "/alerts"));
  }

  for (const s of input.delivery?.system_alerts ?? []) {
    issues.push({
      key: `system:${s.system_kind}`,
      level: CRITICAL_SYSTEM_ALERTS.has(s.system_kind) ? "critical" : "warning",
      tag: "SYSTEM",
      title: systemAlertText(s.system_kind),
      detail: "",
      since: parseTime(s.since),
      href: "/alerts",
    });
  }
  const stuck = input.delivery?.stuck ?? 0;
  if (stuck > 0) {
    issues.push({
      key: "delivery",
      level: "warning",
      tag: "DELIVERY",
      title: `${plural(stuck, "alert notification")} failed or waited more than 15 minutes`,
      detail: "",
      since: null,
      href: "/alerts",
    });
  }

  const age = lastUpdate === null ? 0 : Math.max(0, now - lastUpdate);
  const stale = connection === "live" && lastUpdate !== null && age > 2 * intervalMs;
  if (stale) {
    issues.push({
      key: "stale",
      level: "warning",
      tag: "STALE",
      title: "Data is stale",
      detail: `no update since ${clockTime(lastUpdate)}`,
      since: null,
      href: null,
    });
  }
  if (connection === "reconnecting") {
    issues.push({
      key: "offline",
      level: "warning",
      tag: "OFFLINE",
      title: "Live connection lost",
      detail: "reconnecting to the backend",
      since: null,
      href: null,
    });
  }

  if (connection === "closed") {
    issues.push({
      key: "offline",
      level: "warning",
      tag: "OFFLINE",
      title: "Live connection closed",
      detail: "the session ended: log in again",
      since: null,
      href: null,
    });
  }

  // Stable: critical first, each level in the order added.
  issues.sort((a, b) => Number(b.level === "critical") - Number(a.level === "critical"));
  const level: HealthLevel = issues.some((i) => i.level === "critical")
    ? "critical"
    : issues.length
      ? "warning"
      : "healthy";
  const known =
    (input.instances !== undefined || input.instancesFailed) &&
    (input.alerts !== undefined || input.alertsFailed) &&
    (input.delivery !== undefined || input.deliveryFailed) &&
    !scrape.pending;
  const progress = lastUpdate === null ? 0 : stale ? 1 : Math.min(1, age / intervalMs);
  return { level, issues, known, progress };
}

function unavailable(key: string, title: string, href: string): HealthIssue {
  return {
    key,
    level: "warning",
    tag: "UNAVAILABLE",
    title,
    detail: "the backend didn't answer",
    since: null,
    href,
  };
}
