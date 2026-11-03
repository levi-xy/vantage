// Open now (firing or no data) + history. Visible to every user; the live push
// invalidates ALERTS_KEY. One event per device (series) of a rule; admins can close an
// open event by hand, including one in NO DATA. Vantage's own (system) alerts close by
// themselves.
//
// Only problems get a colour: FIRING critical in the critical tone, warning severity and
// NO DATA amber. RESOLVED rows and history are muted plain text.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api";
import type {
  AlertDelivery,
  AlertEndReason,
  AlertEvent,
  AlertNoDataReason,
  DeliveryStatus,
} from "../../api";
import { useApiClient, useUser } from "../../app/session";
import { ALERTS_KEY } from "../../app/live";
import { ageText, clockTime } from "../../cards/problems";
import { EmptyState } from "../../components/PageHeader";
import { alertError, buttonSecondary } from "../../components/styles";
import { useInstances } from "../devices/queries";
import { NO_DATA_REASONS } from "./noDataReasons";
import { buttonDanger, panel, row, toneText, useNow } from "./tone";
import type { Tone } from "./tone";
import { SectionHeader, Tag } from "./ui";

const PAGE = 50;
const FIRING_LIMIT = 200;

const END_REASONS: Record<AlertEndReason, string> = {
  recovered: "recovered",
  rule_disabled: "rule disabled",
  rule_deleted: "rule deleted",
  rule_changed: "rule changed",
  closed_by_user: "closed by an admin",
};

/** "device not answering", in plain words. */
function noDataWords(reason: AlertNoDataReason | null | undefined): string | null {
  return reason ? (NO_DATA_REASONS[reason] ?? reason.replace(/_/g, " ")) : null;
}

const isOpen = (e: AlertEvent) => e.state === "firing" || e.state === "no_data";

/** The row's tone: firing by severity, no data amber, everything closed muted. */
function eventTone(e: AlertEvent): Tone {
  if (e.state === "no_data") return "warn";
  if (e.state !== "firing") return "muted";
  if (e.severity === "critical") return "crit";
  if (e.severity === "warning") return "warn";
  return "plain";
}

function severityTone(e: AlertEvent): Tone {
  if (!isOpen(e)) return "muted";
  if (e.severity === "critical") return "crit";
  if (e.severity === "warning") return "warn";
  return "muted";
}

const STATE_TAG: Record<string, string> = {
  firing: "FIRING",
  no_data: "NO DATA",
  resolved: "RESOLVED",
};

function parse(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** "14:03" (or "1 Nov 14:03"), the full local date and time in its title. */
function Clock({ iso, now }: { iso: string; now: number }) {
  const t = parse(iso);
  if (t === null) return <>{iso}</>;
  return (
    <time dateTime={iso} title={new Date(t).toLocaleString()} className="tabular-nums">
      {clockTime(t, now)}
    </time>
  );
}

/** "12 min", the start's full local date and time in its title. */
function Age({ iso, now }: { iso: string; now: number }) {
  const t = parse(iso);
  if (t === null) return null;
  return (
    <time dateTime={iso} title={new Date(t).toLocaleString()} className="tabular-nums">
      {ageText(now - t)}
    </time>
  );
}

const DELIVERY_LABELS: Record<DeliveryStatus, string> = {
  pending: "queued",
  sending: "sending",
  delivered: "delivered",
  failed: "failed",
  folded: "sent in a combined message",
  cancelled: "cancelled (channel deleted)",
};

/** Folded into an overflow summary that hasn't been sent yet: not final. */
function inSummary(d: AlertDelivery): boolean {
  return d.status === "folded" && d.finished_at === null;
}

/** "no data", "rule change": the kind in plain words. */
function kindText(d: AlertDelivery): string {
  return d.kind.replace(/_/g, " ");
}

function deliveryText(d: AlertDelivery): string {
  const channel =
    d.channel_name ?? (d.channel_id !== null ? `channel ${d.channel_id}` : "deleted channel");
  if (inSummary(d)) return `${kindText(d)} to ${channel}: queued in summary`;
  const tries = d.attempts === 1 ? "1 attempt" : `${d.attempts} attempts`;
  const error =
    d.error_kind && d.status !== "delivered" ? `, ${d.error_kind.replace(/_/g, " ")}` : "";
  return `${kindText(d)} to ${channel}: ${DELIVERY_LABELS[d.status] ?? d.status} (${tries}${error})`;
}

/** Failed is a problem; a message still waiting after an error is a warning. */
function deliveryTone(d: AlertDelivery): Tone {
  if (d.status === "failed") return "crit";
  if ((d.status === "pending" || d.status === "sending") && d.error_kind) return "warn";
  return "muted";
}

/** "2 notifications · 1 failed": collapsed by default, the full lines on demand. */
function Deliveries({ deliveries }: { deliveries: AlertDelivery[] }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (deliveries.length === 0) return null;
  const failed = deliveries.filter((d) => d.status === "failed").length;
  const waiting = deliveries.filter(
    (d) => d.status === "pending" || d.status === "sending" || inSummary(d),
  ).length;
  const tone: Tone =
    failed > 0 ? "crit" : deliveries.some((d) => deliveryTone(d) === "warn") ? "warn" : "muted";
  const parts = [
    deliveries.length === 1 ? "1 notification" : `${deliveries.length} notifications`,
    failed > 0 ? `${failed} failed` : null,
    waiting > 0 ? `${waiting} waiting` : null,
  ].filter(Boolean);
  return (
    <div className="text-xs">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((o) => !o)}
        className={`inline-flex items-center gap-1 rounded hover:underline focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none ${toneText(tone)}`}
      >
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.4"
          aria-hidden="true"
          className={open ? "rotate-90" : ""}
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
        <span className="tabular-nums">{parts.join(" · ")}</span>
      </button>
      {open && (
        <ul id={listId} aria-label="Notifications" className="mt-1 space-y-0.5 pl-3.5">
          {deliveries.map((d, i) => (
            <li key={i} className={`tabular-nums ${toneText(deliveryTone(d))}`}>
              {deliveryText(d)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Device({ event, names }: { event: AlertEvent; names: Map<string, string> }) {
  const name = event.instance_id ? names.get(event.instance_id) : undefined;
  if (event.instance_id && name) {
    return (
      <Link
        to={`/devices/${encodeURIComponent(event.instance_id)}`}
        className="font-semibold break-words text-ink hover:text-in hover:underline"
      >
        {name}
      </Link>
    );
  }
  return <span className="font-semibold break-words">{event.labels.vantage_instance || "—"}</span>;
}

/** The series' own labels, `key=value`, small. The device is already named. */
function Labels({ labels }: { labels: Record<string, string> }) {
  const shown = Object.entries(labels).filter(([k]) => !k.startsWith("vantage_"));
  if (shown.length === 0) return null;
  return (
    <ul aria-label="Labels" className="flex flex-wrap gap-1">
      {shown.map(([k, v]) => (
        <li
          key={k}
          className="max-w-full truncate rounded bg-chip px-1.5 py-px font-mono text-[11px] text-muted"
          title={`${k}=${v}`}
        >
          {k}={v}
        </li>
      ))}
    </ul>
  );
}

function CloseButton({ event }: { event: AlertEvent }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = useMutation({
    mutationFn: () => api.closeAlertEvent(event.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ALERTS_KEY }),
    onError: (e) => {
      setConfirming(false);
      if (e instanceof ApiError && e.status === 409) {
        void queryClient.invalidateQueries({ queryKey: ALERTS_KEY });
        setError("This alert was already closed.");
      } else {
        setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
      }
    },
  });

  return (
    <div className="flex flex-wrap items-center gap-2 md:justify-end">
      {confirming ? (
        <>
          <button
            type="button"
            className={buttonDanger}
            disabled={close.isPending}
            onClick={() => close.mutate()}
            aria-label={`Confirm closing alert ${event.id}`}
          >
            Confirm close
          </button>
          <button type="button" className={buttonSecondary} onClick={() => setConfirming(false)}>
            Keep open
          </button>
        </>
      ) : (
        <button
          type="button"
          className={buttonSecondary}
          onClick={() => {
            setError(null);
            setConfirming(true);
          }}
          aria-label={`Close alert ${event.id}`}
        >
          Close
        </button>
      )}
      {error && (
        <p role="alert" className="w-full text-xs text-crit-text md:text-right">
          {error}
        </p>
      )}
    </div>
  );
}

/** When: "since 14:03 · 12 min" while open; "14:03 to 15:10 · recovered" once closed. */
function When({ event, now }: { event: AlertEvent; now: number }) {
  if (isOpen(event)) {
    return (
      <>
        since <Clock iso={event.started_at} now={now} /> · <Age iso={event.started_at} now={now} />
      </>
    );
  }
  const reason = event.end_reason ? (END_REASONS[event.end_reason] ?? event.end_reason) : null;
  return (
    <>
      <Clock iso={event.started_at} now={now} />
      {event.ended_at && (
        <>
          {" "}
          to <Clock iso={event.ended_at} now={now} />
        </>
      )}
      {reason && ` · ${reason}`}
    </>
  );
}

function EventItem({
  event,
  names,
  isAdmin,
  now,
}: {
  event: AlertEvent;
  names: Map<string, string>;
  isAdmin: boolean;
  now: number;
}) {
  const open = isOpen(event);
  // Vantage's own alerts have no rule and close by themselves.
  const system = event.source === "system";
  const tone = eventTone(event);
  const reason = event.state === "no_data" ? noDataWords(event.no_data_reason) : null;
  return (
    <li className={row} data-state={event.state}>
      <div className="flex flex-col gap-1.5 md:flex-row md:items-start md:gap-4">
        <div className="flex items-center gap-2 md:w-[88px] md:shrink-0 md:pt-0.5">
          <Tag tone={tone}>{STATE_TAG[event.state] ?? event.state.toUpperCase()}</Tag>
        </div>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            {system ? (
              <span className="font-semibold">Vantage</span>
            ) : (
              <Device event={event} names={names} />
            )}
            <span className={`min-w-0 break-words ${open ? "text-ink" : "text-muted"}`}>
              {event.rule_name}
            </span>
            {system && <span className="text-xs text-muted">system alert, closes by itself</span>}
          </div>
          {event.state === "no_data" && (
            <p className="text-sm text-warn">
              {reason ? `No data: ${reason}` : "No data"}
              {event.state_since && (
                <span className="text-muted">
                  {" "}
                  · no data since <Clock iso={event.state_since} now={now} />
                </span>
              )}
            </p>
          )}
          {event.summary && <p className="text-sm break-words text-muted">{event.summary}</p>}
          <Labels labels={event.labels} />
          <Deliveries deliveries={event.deliveries ?? []} />
        </div>
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm text-muted md:w-[200px] md:shrink-0 md:flex-col md:items-end md:gap-0.5 md:text-right">
          <span className={`${toneText(severityTone(event))} ${open ? "font-medium" : ""}`}>
            {event.severity}
          </span>
          <span className="tabular-nums">
            <When event={event} now={now} />
          </span>
        </div>
        {isAdmin && open && !system && (
          <div className="md:w-[180px] md:shrink-0">
            <CloseButton event={event} />
          </div>
        )}
      </div>
    </li>
  );
}

export function EventsSection() {
  const api = useApiClient();
  const isAdmin = useUser().role === "admin";
  const now = useNow();
  const instances = useInstances();
  const names = new Map((instances.data ?? []).map((i) => [i.id, i.name] as const));
  const firing = useQuery({
    queryKey: [...ALERTS_KEY, "open"],
    queryFn: () => api.openAlertEvents(FIRING_LIMIT),
  });
  const history = useInfiniteQuery({
    queryKey: [...ALERTS_KEY, "history"],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => api.alertEvents({ limit: PAGE, before_id: pageParam }),
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1]?.id : undefined),
  });
  const events = history.data?.pages.flat() ?? [];

  return (
    <>
      <section aria-labelledby="firing-title" className="space-y-2.5">
        <SectionHeader id="firing-title" title="Open now" count={firing.data?.length} />
        {firing.isError ? (
          <p role="alert" className={alertError}>
            Could not load the open alerts.
          </p>
        ) : firing.isPending ? (
          <p role="status" className="text-sm text-muted">
            Loading…
          </p>
        ) : firing.data.length === 0 ? (
          <EmptyState>All clear. Nothing is firing or missing data.</EmptyState>
        ) : (
          <ul aria-label="Open alerts" className={panel}>
            {firing.data.map((e) => (
              <EventItem key={e.id} event={e} names={names} isAdmin={isAdmin} now={now} />
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="history-title" className="space-y-2.5">
        <SectionHeader id="history-title" title="History" />
        {history.isError ? (
          <p role="alert" className={alertError}>
            Could not load the alert history.
          </p>
        ) : history.isPending ? (
          <p role="status" className="text-sm text-muted">
            Loading…
          </p>
        ) : events.length === 0 ? (
          <EmptyState>No alerts yet.</EmptyState>
        ) : (
          <ul aria-label="Alert history" className={panel}>
            {events.map((e) => (
              <EventItem key={e.id} event={e} names={names} isAdmin={false} now={now} />
            ))}
          </ul>
        )}
        {history.hasNextPage && (
          <button
            type="button"
            className={buttonSecondary}
            disabled={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}
          >
            {history.isFetchingNextPage ? "Loading…" : "Load older"}
          </button>
        )}
      </section>
    </>
  );
}
