// Open now (firing or no data) + history. Visible to every user; the live push
// invalidates ALERTS_KEY. One event per device (series) of a rule; admins can close an
// open event by hand, including one in NO DATA.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
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
import { EmptyState } from "../../components/PageHeader";
import { alertError, buttonSecondary } from "../../components/styles";
import { useInstances } from "../devices/queries";

const PAGE = 50;
const FIRING_LIMIT = 200;
const card =
  "rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900";
const buttonDanger = `${buttonSecondary} border-red-400 text-red-700 dark:border-red-700 dark:text-red-300`;

const END_REASONS: Record<AlertEndReason, string> = {
  recovered: "recovered",
  rule_disabled: "rule disabled",
  rule_deleted: "rule deleted",
  rule_changed: "rule changed",
  closed_by_user: "closed by an admin",
};

const NO_DATA_REASONS: Record<string, string> = {
  source_stale: "device not reporting",
  source_down: "device not answering",
  metric_missing: "metric missing",
  query_failed: "query failing",
};

/** "No data: device not answering", in plain words. */
function noDataText(reason: AlertNoDataReason | null | undefined): string {
  return reason ? `No data: ${NO_DATA_REASONS[reason] ?? reason.replace(/_/g, " ")}` : "No data";
}

const STATE_STYLE: Record<string, string> = {
  firing: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  no_data: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  resolved: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
};

function stateLabel(event: AlertEvent): string {
  if (event.state === "firing") return "Firing";
  if (event.state === "no_data") return noDataText(event.no_data_reason);
  return "Resolved";
}

const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  warning: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
};
const SEVERITY_DEFAULT = "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

const DELIVERY_LABELS: Record<DeliveryStatus, string> = {
  pending: "queued",
  sending: "sending",
  delivered: "delivered",
  failed: "failed",
  folded: "sent in a combined message",
  cancelled: "cancelled (channel deleted)",
};

const WAITING_STYLE = "text-amber-700 dark:text-amber-300";

const DELIVERY_STYLE: Partial<Record<DeliveryStatus, string>> = {
  delivered: "text-emerald-700 dark:text-emerald-300",
  folded: "text-emerald-700 dark:text-emerald-300",
  failed: "text-red-700 dark:text-red-300",
  pending: WAITING_STYLE,
  sending: WAITING_STYLE,
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

function deliveryStyle(d: AlertDelivery): string {
  if (inSummary(d)) return WAITING_STYLE;
  return DELIVERY_STYLE[d.status] ?? "text-slate-600 dark:text-slate-400";
}

/** The messages about this event, one short line each. */
function Deliveries({ deliveries }: { deliveries: AlertDelivery[] }) {
  if (deliveries.length === 0) return null;
  return (
    <ul aria-label="Notifications" className="space-y-0.5 text-xs">
      {deliveries.map((d, i) => (
        <li key={i} className={deliveryStyle(d)}>
          {deliveryText(d)}
        </li>
      ))}
    </ul>
  );
}

function Device({ event, names }: { event: AlertEvent; names: Map<string, string> }) {
  const name = event.instance_id ? names.get(event.instance_id) : undefined;
  if (event.instance_id && name) {
    return (
      <Link
        to={`/devices/${encodeURIComponent(event.instance_id)}`}
        className="font-medium break-words text-sky-700 underline dark:text-sky-300"
      >
        {name}
      </Link>
    );
  }
  return <span className="font-medium break-words">{event.labels.vantage_instance || "—"}</span>;
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
    <div className="flex flex-wrap items-center gap-2">
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
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          {error}
        </p>
      )}
    </div>
  );
}

function EventItem({
  event,
  names,
  isAdmin,
}: {
  event: AlertEvent;
  names: Map<string, string>;
  isAdmin: boolean;
}) {
  const open = event.state === "firing" || event.state === "no_data";
  // Vantage's own alerts have no rule and close by themselves.
  const system = event.source === "system";
  return (
    <li className={card}>
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                STATE_STYLE[event.state] ?? SEVERITY_DEFAULT
              }`}
            >
              {stateLabel(event)}
            </span>
            <span
              className={`rounded px-2 py-0.5 text-xs ${SEVERITY_STYLE[event.severity] ?? SEVERITY_DEFAULT}`}
            >
              {event.severity}
            </span>
            {system ? (
              <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                Vantage
              </span>
            ) : (
              <Device event={event} names={names} />
            )}
            <span className="min-w-0 text-sm break-words text-slate-700 dark:text-slate-300">
              {event.rule_name}
            </span>
          </div>
          <p className="text-sm break-words">{event.summary}</p>
          <p className="text-xs text-slate-600 dark:text-slate-400">
            Since {when(event.started_at)}
            {event.state === "no_data" && event.state_since
              ? ` · no data since ${when(event.state_since)}`
              : ""}
            {event.ended_at ? ` · resolved ${when(event.ended_at)}` : ""}
            {event.end_reason ? ` · ${END_REASONS[event.end_reason] ?? event.end_reason}` : ""}
          </p>
          <Deliveries deliveries={event.deliveries ?? []} />
        </div>
        {isAdmin && open && !system && <CloseButton event={event} />}
      </div>
    </li>
  );
}

export function EventsSection() {
  const api = useApiClient();
  const isAdmin = useUser().role === "admin";
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
      <section aria-labelledby="firing-title" className="space-y-3">
        <h2 id="firing-title" className="text-base font-semibold">
          Open now
        </h2>
        {firing.isError ? (
          <p role="alert" className={alertError}>
            Could not load the open alerts.
          </p>
        ) : firing.isPending ? (
          <p role="status" className="text-sm">
            Loading…
          </p>
        ) : firing.data.length === 0 ? (
          <EmptyState>All clear. Nothing is firing or missing data.</EmptyState>
        ) : (
          <ul aria-label="Open alerts" className="space-y-2">
            {firing.data.map((e) => (
              <EventItem key={e.id} event={e} names={names} isAdmin={isAdmin} />
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="history-title" className="space-y-3">
        <h2 id="history-title" className="text-base font-semibold">
          History
        </h2>
        {history.isError ? (
          <p role="alert" className={alertError}>
            Could not load the alert history.
          </p>
        ) : history.isPending ? (
          <p role="status" className="text-sm">
            Loading…
          </p>
        ) : events.length === 0 ? (
          <EmptyState>No alerts yet.</EmptyState>
        ) : (
          <ul aria-label="Alert history" className="space-y-2">
            {events.map((e) => (
              <EventItem key={e.id} event={e} names={names} isAdmin={false} />
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
