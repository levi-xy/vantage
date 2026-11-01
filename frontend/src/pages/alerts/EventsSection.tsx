// Firing now + history. Visible to every user; the live push invalidates ALERTS_KEY.

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { AlertEvent } from "../../api";
import { useApiClient } from "../../app/session";
import { ALERTS_KEY } from "../../app/live";
import { EmptyState } from "../../components/PageHeader";
import { alertError, buttonSecondary } from "../../components/styles";

const PAGE = 50;
const FIRING_LIMIT = 200;
const card =
  "rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900";

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function EventItem({ event }: { event: AlertEvent }) {
  const firing = event.state === "firing";
  return (
    <li className={card}>
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
        <span className="min-w-0 text-sm break-words">{event.summary}</span>
      </div>
      <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
        Since {when(event.started_at)}
        {event.ended_at ? ` · resolved ${when(event.ended_at)}` : ""}
      </p>
    </li>
  );
}

export function EventsSection() {
  const api = useApiClient();
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
              <EventItem key={e.id} event={e} />
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
              <EventItem key={e.id} event={e} />
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
