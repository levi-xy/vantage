import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import type { Instance } from "../api";
import { shownState, useScrapeStatus } from "../cards/metrics/scrapeStatus";
import { useOpenAlerts } from "../cards/openAlerts";
import { healthCounts } from "../cards/overview";
import { findProblems, normalSubline, problemSubline, sinceText } from "../cards/problems";
import type { Problem, ProblemTag } from "../cards/problems";

const MUTED = "text-muted";
const CRIT = "text-crit-text";

/** The current time, updated every second (for "checked 12 s ago"). */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

const devices = (n: number) => `${n} ${n === 1 ? "device" : "devices"}`;

/** The newest poll check of any device, or the list's own update time. */
function lastChecked(instances: Instance[], listUpdatedAt: number): number | null {
  let newest: number | null = null;
  for (const i of instances) {
    const t = i.status.checked_at ? Date.parse(i.status.checked_at) : NaN;
    if (!Number.isNaN(t) && (newest === null || t > newest)) newest = t;
  }
  return newest ?? (listUpdatedAt || null);
}

const TAG_STYLE: Record<ProblemTag, string> = {
  DOWN: CRIT,
  FIRING: CRIT,
  "NO DATA": "text-warn",
};

function ProblemRow({ problem, nowMs }: { problem: Problem; nowMs: number }) {
  const since = sinceText(problem.since, nowMs);
  return (
    <li className="flex min-h-11 flex-wrap items-center gap-x-3.5 gap-y-1 border-t border-crit/30 px-4 py-2.5 first:border-t-0 md:px-4.5 md:py-3">
      <span
        className={`rounded border border-current px-1.5 py-0.5 text-[11px] leading-none font-medium tracking-wide ${TAG_STYLE[problem.tag]}`}
      >
        {problem.tag}
      </span>
      <span className="min-w-0 font-medium">{problem.title}</span>
      {problem.detail && (
        <span className={`hidden min-w-0 truncate md:inline ${MUTED}`}>{problem.detail}</span>
      )}
      <span className="ml-auto flex items-center gap-3.5">
        {since && <span className={`text-xs tabular-nums ${MUTED}`}>{since}</span>}
        <Link
          to={problem.href}
          className="text-sm text-in hover:underline"
          aria-label={`Open ${problem.title}`}
        >
          Open
        </Link>
      </span>
    </li>
  );
}

/**
 * The dashboard's headline ("All systems normal" or "2 problems") and, when something is wrong,
 * the problems strip under it. Instances and open alerts come from the shared queries, which
 * `/ws` pushes keep current. Renders nothing until both have answered (or alerts failed).
 */
export function DashboardHeadline({
  instances,
  listUpdatedAt,
  actions,
}: {
  instances: Instance[] | undefined;
  /** When the instance list was last fetched or pushed. */
  listUpdatedAt: number;
  actions?: ReactNode;
}) {
  const nowMs = useNow();
  const alerts = useOpenAlerts();
  const scrape = useScrapeStatus(instances ?? []);
  const list = instances ?? [];
  const ready =
    instances !== undefined && (alerts.data !== undefined || alerts.isError) && !scrape.pending;

  let headline = "";
  let subline = "";
  let problems: Problem[] = [];
  if (ready) {
    const stateOf = (i: Instance) => shownState(i, scrape.states);
    problems = findProblems(list, alerts.data ?? [], stateOf, nowMs);
    // Enabled devices nobody could judge: scrape-only ones while Prometheus fails, or not
    // checked yet. They are never counted as up.
    const unknown = list.filter((i) => i.enabled && stateOf(i) === "unknown").length;
    if (problems.length) {
      headline = `${problems.length} ${problems.length === 1 ? "problem" : "problems"}`;
      subline = problemSubline(problems);
      if (scrape.failed && unknown) subline += ` Status unknown for ${devices(unknown)}.`;
    } else if (list.length === 0) {
      headline = "No devices yet";
      subline = "Add a device to start monitoring.";
    } else {
      const counts = healthCounts(list, stateOf);
      const checked = lastChecked(list, listUpdatedAt);
      const rest = normalSubline(
        counts.up,
        counts.pending,
        scrape.failed ? 0 : unknown,
        !alerts.isError,
        checked === null ? null : nowMs - checked,
      );
      if (scrape.failed && unknown) {
        headline = `Status unknown for ${devices(unknown)}`;
        subline = `Prometheus can't be reached · ${rest}`;
      } else {
        // With the alert list unreadable, "normal" can't be claimed.
        headline = alerts.isError ? "No device problems" : "All systems normal";
        subline = rest;
      }
    }
  }

  return (
    <div className="mb-4 flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-4">
        <div className="flex min-w-0 flex-col gap-1.5">
          {/* The page's own heading stays "Dashboard"; the headline is the status under it. */}
          <h1 className="sr-only">Dashboard</h1>
          <h2
            aria-live="polite"
            className={`text-[21px] font-semibold tracking-tight md:text-[26px] ${
              problems.length ? CRIT : ""
            }`}
          >
            {headline || "Dashboard"}
          </h2>
          {subline && <p className={MUTED}>{subline}</p>}
        </div>
        {actions && <div className="ml-auto">{actions}</div>}
      </div>
      {problems.length > 0 && (
        <section
          aria-label="Active problems"
          className="overflow-hidden rounded-xl border border-crit/40 bg-crit/5"
        >
          <ul>
            {problems.map((p) => (
              <ProblemRow key={p.key} problem={p} nowMs={nowMs} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
