// Firing now + history. Visible to every user; the live push invalidates ALERTS_KEY.
// One event per device (series) of a rule; admins can close an open event by hand.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api";
import type { AlertEndReason, AlertEvent } from "../../api";
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

const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  warning: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
};
const SEVERITY_DEFAULT = "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
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
  const firing = event.state === "firing";
  return (
    <li className={card}>
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                firing
                  ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200"
                  : "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
              }`}
            >
              {firing ? "Firing" : "Resolved"}
            </span>
            <span
              className={`rounded px-2 py-0.5 text-xs ${SEVERITY_STYLE[event.severity] ?? SEVERITY_DEFAULT}`}
            >
              {event.severity}
            </span>
            <Device event={event} names={names} />
            <span className="min-w-0 text-sm break-words text-slate-700 dark:text-slate-300">
              {event.rule_name}
            </span>
          </div>
          <p className="text-sm break-words">{event.summary}</p>
          <p className="text-xs text-slate-600 dark:text-slate-400">
            Since {when(event.started_at)}
            {event.ended_at ? ` · resolved ${when(event.ended_at)}` : ""}
            {event.end_reason ? ` · ${END_REASONS[event.end_reason] ?? event.end_reason}` : ""}
          </p>
        </div>
        {isAdmin && firing && <CloseButton event={event} />}
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
    queryKey: [...ALERTS_KEY, "firing"],
    queryFn: () => api.alertEvents({ state: "firing", limit: FIRING_LIMIT }),
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
          Firing now
        </h2>
        {firing.isError ? (
          <p role="alert" className={alertError}>
            Could not load the firing alerts.
          </p>
        ) : firing.isPending ? (
          <p role="status" className="text-sm">
            Loading…
          </p>
        ) : firing.data.length === 0 ? (
          <EmptyState>All clear. Nothing is firing.</EmptyState>
        ) : (
          <ul aria-label="Firing alerts" className="space-y-2">
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
