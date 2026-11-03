// The problems the top bar's health status lists. Pure functions, so they're easy to test.

import type { AlertEvent, Instance } from "../api";
import { NO_DATA_REASONS } from "../pages/alerts/noDataReasons";

type State = Instance["status"]["state"];

export type ProblemTag = "DOWN" | "FIRING" | "NO DATA";

export interface Problem {
  key: string;
  tag: ProblemTag;
  /** "VyOS router is down", or the rule's name. */
  title: string;
  /** Short and secret-free: the collector's error, the device's name or the no-data reason. */
  detail: string;
  /** Epoch milliseconds, or null when unknown. */
  since: number | null;
  /** Critical: a down device or a firing alert of severity critical. Everything else warns. */
  critical: boolean;
  /** Where "Open" goes. There is no per-device page yet, so devices go to the device list. */
  href: string;
}

function noDataWords(reason: string | null | undefined): string {
  if (!reason) return "no data";
  return NO_DATA_REASONS[reason] ?? reason.replace(/_/g, " ");
}

function parseTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "14:03" in local time; "1 Nov 14:03" when it's not today. */
export function clockTime(ms: number, nowMs: number): string {
  const d = new Date(ms);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === new Date(nowMs).toDateString()
    ? time
    : `${d.getDate()} ${MONTHS[d.getMonth()]} ${time}`;
}

/** "12 s", "12 min", "2 h 5 min", "3 d". */
export function ageText(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s} s`;
  const min = Math.floor(s / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return min % 60 ? `${h} h ${min % 60} min` : `${h} h`;
  return `${Math.floor(h / 24)} d`;
}

/** "since 14:03 · 12 min", or "" when the start is unknown. */
export function sinceText(since: number | null, nowMs: number): string {
  return since === null ? "" : `since ${clockTime(since, nowMs)} · ${ageText(nowMs - since)}`;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/**
 * Down devices plus open alert events, oldest first. Alerts about a device that is down are
 * folded into that device's row (they're one problem: the device stopped answering).
 * `stateOf` gives the state to judge (the backend's, or Prometheus' for unpolled devices).
 */
export function findProblems(
  instances: Instance[],
  events: AlertEvent[],
  stateOf: (i: Instance) => State,
): Problem[] {
  const names = new Map(instances.map((i) => [i.id, i.name] as const));
  const down = instances.filter((i) => i.enabled && stateOf(i) === "down");
  const folded = new Map<string, number>();
  for (const e of events) {
    if (e.instance_id && down.some((i) => i.id === e.instance_id))
      folded.set(e.instance_id, (folded.get(e.instance_id) ?? 0) + 1);
  }

  const problems: Problem[] = down.map((i) => {
    // Polled devices: when it last answered. Scrape-only devices: unknown here.
    const since = i.status.state === "down" ? parseTime(i.status.last_success_at) : null;
    const extra = folded.get(i.id);
    const reason =
      i.status.state === "down"
        ? (i.status.error ?? "not answering")
        : "Prometheus can't scrape it";
    return {
      key: `instance:${i.id}`,
      tag: "DOWN",
      title: `${i.name} is down`,
      detail: extra ? `${reason} · ${plural(extra, "open alert")}` : reason,
      since,
      critical: true,
      href: "/devices",
    };
  });

  for (const e of events) {
    if (e.instance_id && folded.has(e.instance_id)) continue;
    // Vantage's own alerts come from the delivery status (one SYSTEM issue each, at its own
    // level); counted here too they showed twice and raised Prometheus being away to
    // Critical through the event's severity.
    if (e.source === "system") continue;
    const device = e.instance_id ? names.get(e.instance_id) : undefined;
    if (e.state === "no_data") {
      const since = parseTime(e.state_since) ?? parseTime(e.started_at);
      const words = noDataWords(e.no_data_reason);
      problems.push({
        key: `event:${e.id}`,
        tag: "NO DATA",
        title: e.rule_name,
        detail: device ? `${words} · ${device}` : words,
        since,
        critical: false,
        href: "/alerts",
      });
    } else if (e.state === "firing") {
      const since = parseTime(e.started_at);
      problems.push({
        key: `event:${e.id}`,
        tag: "FIRING",
        title: e.rule_name,
        detail: device ?? e.summary,
        since,
        critical: e.severity === "critical",
        href: "/alerts",
      });
    }
  }

  return problems.sort(
    (a, b) => (a.since ?? Infinity) - (b.since ?? Infinity) || a.title.localeCompare(b.title),
  );
}
