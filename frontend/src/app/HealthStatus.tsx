import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { useScrapeStatus } from "../cards/metrics/scrapeStatus";
import { useOpenAlerts } from "../cards/openAlerts";
import { sinceText } from "../cards/problems";
import { useInstances } from "../pages/devices/queries";
import { computeHealth } from "./health";
import type { HealthIssue, HealthLevel } from "./health";
import { useDeliveryStatus } from "./deliveryStatus";
import { clockTime } from "./live";
import type { LiveState } from "./live";

const RING = 2 * Math.PI * 5.5;

const LABEL: Record<HealthLevel, string> = {
  healthy: "Healthy",
  warning: "Warning",
  critical: "Critical",
};
const STROKE: Record<HealthLevel, string> = {
  healthy: "stroke-ok",
  warning: "stroke-warn",
  critical: "stroke-crit",
};
const FILL: Record<HealthLevel, string> = {
  healthy: "fill-ok",
  warning: "fill-warn",
  critical: "fill-crit",
};
// Healthy stays muted: only problems get a colour.
const TEXT: Record<HealthLevel, string> = {
  healthy: "text-muted",
  warning: "text-warn",
  critical: "text-crit-text",
};
const TAG_TEXT = { warning: "text-warn", critical: "text-crit-text" } as const;

const focusRing =
  "focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none focus-visible:ring-inset";

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function IssueRow({ issue, now, onOpen }: { issue: HealthIssue; now: number; onOpen: () => void }) {
  const since = sinceText(issue.since, now);
  return (
    <li className="flex flex-col gap-0.5 border-t border-line px-4 py-2.5 first:border-t-0">
      <div className="flex items-center gap-2.5">
        <span
          className={`shrink-0 rounded border border-current px-1.5 py-0.5 text-[11px] leading-none font-medium tracking-wide ${TAG_TEXT[issue.level]}`}
        >
          {issue.tag}
        </span>
        <span className="min-w-0 font-medium">{issue.title}</span>
        {issue.href && (
          <Link
            to={issue.href}
            onClick={onOpen}
            className="ml-auto shrink-0 text-sm text-in hover:underline"
            aria-label={`Open ${issue.title}`}
          >
            Open
          </Link>
        )}
      </div>
      {(issue.detail || since) && (
        <p className="text-xs break-words text-muted">
          {[issue.detail, since].filter(Boolean).join(" · ")}
        </p>
      )}
    </li>
  );
}

/**
 * The top bar's health status: Healthy, Warning or Critical, with a ring that fills until
 * the next refresh. A button; its popover lists every problem behind the level.
 */
export function HealthStatus({
  connection,
  lastUpdate,
}: {
  connection: LiveState;
  lastUpdate: number | null;
}) {
  const now = useNow();
  const instances = useInstances();
  const alerts = useOpenAlerts();
  const scrape = useScrapeStatus(instances.data ?? []);
  const delivery = useDeliveryStatus();
  const health = computeHealth({
    connection,
    lastUpdate,
    now,
    instances: instances.data,
    instancesFailed: instances.isError,
    alerts: alerts.data,
    alertsFailed: alerts.isError,
    scrape,
    delivery: delivery.data,
    deliveryFailed: delivery.isError,
  });
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Close on Escape (focus back on the button) or on a click outside the button and panel.
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const inside = [buttonRef, panelRef].some((r) => r.current?.contains(target));
      if (!inside) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Until devices, alerts and delivery status have answered, nothing is claimed (unless a problem is known).
  const checking = !health.known && health.issues.length === 0;
  const label = checking ? "Checking" : LABEL[health.level];
  const time = lastUpdate === null ? null : clockTime(lastUpdate);
  const tone = checking ? "text-muted" : TEXT[health.level];

  return (
    <div className="relative">
      <span aria-live="polite" className="sr-only">
        {checking ? "" : `Health: ${label}`}
      </span>
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls="health-panel"
        aria-label={`Health: ${label}. Show details`}
        data-level={checking ? "checking" : health.level}
        onClick={() => setOpen((o) => !o)}
        className={`flex h-9 items-center gap-[7px] rounded-lg px-2 text-[13px] whitespace-nowrap hover:bg-chip ${tone} ${focusRing}`}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
          <circle cx="7" cy="7" r="5.5" strokeWidth="2" className="stroke-track" />
          {!checking && health.progress > 0 && (
            <circle
              cx="7"
              cy="7"
              r="5.5"
              strokeWidth="2"
              strokeLinecap="round"
              strokeDasharray={`${(health.progress * RING).toFixed(2)} ${RING.toFixed(2)}`}
              transform="rotate(-90 7 7)"
              className={`${STROKE[health.level]} transition-[stroke-dasharray] duration-1000 ease-linear motion-reduce:transition-none`}
            />
          )}
          <circle cx="7" cy="7" r="2" className={checking ? "fill-muted" : FILL[health.level]} />
        </svg>
        <span className={health.level === "healthy" || checking ? "" : "font-medium"}>{label}</span>
        {time && <span className="hidden text-muted md:inline">· {time}</span>}
      </button>
      {open && (
        <div
          id="health-panel"
          ref={panelRef}
          role="dialog"
          aria-label="System health"
          className="fixed inset-x-0 top-14 z-40 max-h-[70vh] overflow-y-auto border-b border-line bg-panel text-sm text-ink shadow-lg md:absolute md:top-full md:right-0 md:left-auto md:mt-2 md:w-[400px] md:rounded-lg md:border"
        >
          {health.issues.length > 0 ? (
            <>
              <ul>
                {health.issues.map((issue) => (
                  <IssueRow key={issue.key} issue={issue} now={now} onOpen={() => setOpen(false)} />
                ))}
              </ul>
              {time && (
                <p className="border-t border-line px-4 py-2 text-xs text-muted">
                  Last update {time}
                </p>
              )}
            </>
          ) : (
            <p className="px-4 py-3 text-muted">
              {checking
                ? "Checking devices and alerts…"
                : `Everything is healthy${time ? ` · last update ${time}` : ""}`}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
