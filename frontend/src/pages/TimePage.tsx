// Settings (admin only). "Time" (maintainer-approved 2026-11-07): the one timezone every
// time in Vantage is shown in. The clock itself is the host's; Vantage never sets it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import type { TimeSettings } from "../api";
import { useApiClient } from "../app/session";
import { formatDateTime } from "../app/time";
import { TIME_KEY } from "../app/timeSettings";
import { PageHeader } from "../components/PageHeader";
import { alertError, buttonPrimary, fieldHint, input, label } from "../components/styles";

const MAX_LISTED = 200;
const SKEW_WARN_MS = 5000;

/** "42 s", "3 min". */
function duration(ms: number): string {
  const s = Math.round(Math.abs(ms) / 1000);
  return s < 120 ? `${s} s` : `${Math.round(s / 60)} min`;
}

function skewWarning(skewMs: number): string | null {
  if (Math.abs(skewMs) <= SKEW_WARN_MS) return null;
  const side = skewMs < 0 ? "behind" : "ahead of";
  return `The server clock is ${duration(skewMs)} ${side} this browser. Check NTP on the host.`;
}

function TimeSection({ data, updatedAt }: { data: TimeSettings; updatedAt: number }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [chosen, setChosen] = useState(data.timezone);
  const [filter, setFilter] = useState("");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const save = useMutation({
    mutationFn: (timezone: string) => api.setTimeZone(timezone),
    onSuccess: (result) => {
      queryClient.setQueryData(TIME_KEY, result);
    },
  });

  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const all = q ? data.timezones.filter((z) => z.toLowerCase().includes(q)) : data.timezones;
    // The current choice stays in the list whatever the filter says.
    return all.includes(chosen) ? all : [chosen, ...all];
  }, [data.timezones, filter, chosen]);

  // The server's time when the data arrived, moved on by the time since then.
  const skewMs = Date.parse(data.server_time) - updatedAt;
  const serverNow = now + skewMs;
  const warning = skewWarning(skewMs);

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (save.isPending) return;
    save.mutate(chosen);
  }

  return (
    <section
      aria-labelledby="time-title"
      className="max-w-lg rounded-lg border border-line bg-panel p-4"
    >
      <h2 id="time-title" className="mb-1 text-base font-semibold">
        Time
      </h2>
      <p className="mb-4 text-sm text-muted">
        Everyone sees times in this timezone: web pages, Telegram messages, logs and the command
        line. Stored data stays in UTC.
      </p>
      <form onSubmit={onSubmit} className="space-y-4">
        <div>
          <label htmlFor="tz-filter" className={label}>
            Search timezones
          </label>
          <input
            id="tz-filter"
            type="search"
            className={input}
            value={filter}
            placeholder="Europe, Berlin, ..."
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="tz-select" className={label}>
            Timezone
          </label>
          <select
            id="tz-select"
            className={input}
            value={chosen}
            onChange={(e) => {
              setChosen(e.target.value);
            }}
          >
            {matches.slice(0, MAX_LISTED).map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
          {matches.length > MAX_LISTED && (
            <p className={fieldHint}>
              Showing {MAX_LISTED} of {matches.length}. Search to narrow down.
            </p>
          )}
        </div>
        <p className="text-sm">
          Server time in {chosen}:{" "}
          <span className="font-medium tabular-nums" data-testid="server-time">
            {formatDateTime(serverNow, chosen)}
          </span>
        </p>
        <p className="text-sm text-muted">
          Clock source: the host&apos;s clock. Keep NTP running on the host (chrony or
          systemd-timesyncd).
        </p>
        {warning && (
          <p role="alert" className={alertError}>
            {warning}
          </p>
        )}
        {save.isError && (
          <p role="alert" className={alertError}>
            Could not save the timezone. Try again.
          </p>
        )}
        <button type="submit" className={buttonPrimary} disabled={save.isPending}>
          Save
        </button>
      </form>
    </section>
  );
}

export function TimePage() {
  const api = useApiClient();
  const time = useQuery({
    queryKey: TIME_KEY,
    queryFn: () => api.timeSettings(),
    staleTime: 60_000,
  });
  return (
    <>
      <PageHeader title="Time" />
      {time.isError && (
        <p role="alert" className={alertError}>
          Could not load the settings.
        </p>
      )}
      {time.data && Array.isArray(time.data.timezones) && (
        <TimeSection
          // Saving changes the zone, which remounts the page (Layout) with the new value.
          key={time.data.timezone}
          data={time.data}
          updatedAt={time.dataUpdatedAt}
        />
      )}
    </>
  );
}
