import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import type { Instance, TestResult } from "../api";
import { deviceColor } from "../app/deviceColors";
import { useApiClient, useUser } from "../app/session";
import { DeviceTypeIcon } from "../cards/metrics/CardHeader";
import { shownState, useScrapeStates } from "../cards/metrics/scrapeStatus";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError, buttonPrimary } from "../components/styles";
import { deviceHealth, timeText, TONE_CLASS } from "./devices/deviceStatus";
import type { DeviceHealth } from "./devices/deviceStatus";
import { TestOutcome } from "./devices/InstanceForm";
import { errorText, INSTANCES_KEY, useInstances, useIntegrations } from "./devices/queries";
import { StateText } from "./devices/StateText";

const focusRing = "focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none";

const actionButton = `inline-flex h-8 items-center justify-center rounded-md border border-line px-2.5 text-[13px] text-ink hover:bg-chip disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`;

// Columns on desktop: device, state, last error, last success (and the admin actions).
// Every track has a fixed or fr width (no `auto`), so each row, which is its own grid, and
// the column header line up. The header and the rows share these strings, padding included.
const COLUMNS = {
  admin: "gap-x-4 px-4 md:grid-cols-[minmax(0,1.2fr)_6.5rem_minmax(0,1.4fr)_11rem_16rem]",
  viewer: "gap-x-4 px-4 md:grid-cols-[minmax(0,1.2fr)_6.5rem_minmax(0,1.4fr)_11rem]",
};
// Phones: the device and its state on one line, the rest below.
const rowGrid = "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-y-1";
// Phone lines under the device line start under its name, not its icon.
const underName = "max-md:pl-[46px]";

const NO_SITE = "No site";

/** Now in epoch ms, updated every 30 s (for "12 min ago"). */
function useClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

function AdminActions({ instance }: { instance: Instance }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TestResult | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: INSTANCES_KEY });

  const toggle = useMutation({
    mutationFn: () => api.updateInstance(instance.id, { enabled: !instance.enabled }),
    onSuccess: refresh,
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteInstance(instance.id),
    onSuccess: refresh,
    onError: (e) => setError(errorText(e)),
  });
  const test = useMutation({
    mutationFn: () => api.testInstance(instance.id),
    onSuccess: setResult,
    onError: (e) => setError(errorText(e)),
  });
  const busy = toggle.isPending || remove.isPending || test.isPending;

  function run(action: { mutate: () => void }) {
    setError(null);
    setResult(null);
    action.mutate();
  }

  return (
    <>
      <div
        className={`col-span-full flex flex-wrap gap-1.5 md:col-span-1 md:justify-end ${underName}`}
      >
        {confirming ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(remove)}
              className={`${actionButton} border-crit text-crit-text`}
              aria-label={`Confirm deleting ${instance.name}`}
            >
              Confirm delete
            </button>
            <button type="button" onClick={() => setConfirming(false)} className={actionButton}>
              Keep
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(test)}
              className={actionButton}
              aria-label={`Test ${instance.name}`}
            >
              {test.isPending ? "Testing…" : "Test"}
            </button>
            <Link
              to={`/devices/${encodeURIComponent(instance.id)}/edit`}
              className={actionButton}
              aria-label={`Edit ${instance.name}`}
            >
              Edit
            </Link>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(toggle)}
              className={actionButton}
              aria-label={`${instance.enabled ? "Disable" : "Enable"} ${instance.name}`}
            >
              {instance.enabled ? "Disable" : "Enable"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(true)}
              className={actionButton}
              aria-label={`Delete ${instance.name}`}
            >
              Delete
            </button>
          </>
        )}
      </div>
      {(error || result) && (
        <div className={`col-span-full space-y-2 pt-1 ${underName}`}>
          {error && (
            <p role="alert" className={alertError}>
              {error}
            </p>
          )}
          {result && <TestOutcome result={result} />}
        </div>
      )}
    </>
  );
}

interface Row {
  instance: Instance;
  health: DeviceHealth;
}

function DeviceRow({
  row,
  typeName,
  isAdmin,
  now,
}: {
  row: Row;
  typeName: string;
  isAdmin: boolean;
  now: number;
}) {
  const { instance, health } = row;
  const { status } = instance;
  const checked = timeText(status.checked_at, now);
  const last = timeText(status.last_success_at, now);
  const color = deviceColor(instance);
  let lastText: string;
  if (last) lastText = last.short;
  else if (health.scraped) lastText = "scraped by Prometheus";
  else lastText = instance.enabled ? "never" : "—";

  return (
    <li
      className={`${rowGrid} ${isAdmin ? COLUMNS.admin : COLUMNS.viewer} py-2.5 ${instance.enabled ? "" : "text-muted"}`}
    >
      <div className="flex min-w-0 items-center gap-3">
        <DeviceTypeIcon typeId={instance.type_id} />
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            {color && (
              <span
                data-testid="device-swatch"
                role="img"
                aria-label={`Colour: ${instance.color}`}
                style={{ backgroundColor: color }}
                className="h-2.5 w-2.5 shrink-0 rounded-full"
              />
            )}
            <Link
              to={`/devices/${encodeURIComponent(instance.id)}`}
              state={{ from: "/devices" }}
              className="truncate text-[15px] font-semibold hover:text-in hover:underline"
              title={instance.name}
            >
              {instance.name}
            </Link>
          </div>
          <div className="truncate text-xs text-muted">
            {typeName}
            {instance.tags.length > 0 && ` · ${instance.tags.join(", ")}`}
          </div>
        </div>
      </div>
      <div className="justify-self-end md:justify-self-start">
        <StateText health={health} title={checked ? `Checked ${checked.full}` : undefined} />
      </div>
      <div className={`col-span-full min-w-0 max-md:empty:hidden md:col-span-1 ${underName}`}>
        {health.problem && (
          <span
            title={health.problem}
            className={`block truncate text-xs ${health.problemTone ? TONE_CLASS[health.problemTone] : "text-muted"}`}
          >
            {health.problem}
          </span>
        )}
      </div>
      <div
        className={`col-span-full truncate text-xs text-muted md:col-span-1 ${underName}`}
        title={last?.full}
      >
        <span className="md:sr-only">Last success </span>
        {lastText}
      </div>
      {isAdmin && <AdminActions instance={instance} />}
    </li>
  );
}

interface Group {
  site: string;
  rows: Row[];
  /** The group's most urgent row (see DeviceHealth.rank). */
  rank: number;
}

/** By site, problems first: sites with a down or failing device on top, "No site" last. */
function groupBySite(rows: Row[]): Group[] {
  const bySite = new Map<string, Row[]>();
  for (const row of rows) {
    // An empty site is no site (vantage_site="" in Prometheus).
    const site = row.instance.site || NO_SITE;
    bySite.set(site, [...(bySite.get(site) ?? []), row]);
  }
  const groups = [...bySite].map(([site, list]) => {
    const sorted = list.sort(
      (a, b) => a.health.rank - b.health.rank || a.instance.name.localeCompare(b.instance.name),
    );
    return { site, rows: sorted, rank: Math.min(sorted[0]?.health.rank ?? 2, 2) };
  });
  return groups.sort(
    (a, b) =>
      a.rank - b.rank ||
      Number(a.site === NO_SITE) - Number(b.site === NO_SITE) ||
      a.site.localeCompare(b.site),
  );
}

function counts(rows: Row[]) {
  return {
    down: rows.filter((r) => r.health.rank === 0).length,
    failing: rows.filter((r) => r.health.rank === 1).length,
  };
}

/** "3 devices · 1 down · 1 failing"; only the problems are coloured. */
function Summary({ rows }: { rows: Row[] }) {
  const { down, failing } = counts(rows);
  return (
    <span className="text-xs text-muted">
      {rows.length === 1 ? "1 device" : `${rows.length} devices`}
      {down > 0 && (
        <>
          {" · "}
          <span className={TONE_CLASS.crit}>{down} down</span>
        </>
      )}
      {failing > 0 && (
        <>
          {" · "}
          <span className={TONE_CLASS.warn}>{failing} failing</span>
        </>
      )}
    </span>
  );
}

/** The list of devices (instances). Admins can add, test, edit, disable and delete them. */
export function DevicesPage() {
  const user = useUser();
  const isAdmin = user.role === "admin";
  const instances = useInstances();
  const scrape = useScrapeStates(instances.data ?? []);
  const integrations = useIntegrations();
  const now = useClock();
  const typeName = (typeId: string) =>
    integrations.data?.find((t) => t.type_id === typeId)?.name ?? typeId;
  const rows: Row[] = (instances.data ?? []).map((instance) => ({
    instance,
    health: deviceHealth(instance, shownState(instance, scrape)),
  }));

  let body;
  if (instances.isPending) {
    body = (
      <p role="status" className="text-sm text-muted">
        Loading…
      </p>
    );
  } else if (instances.isError) {
    body = (
      <p role="alert" className={alertError}>
        {errorText(instances.error)}
      </p>
    );
  } else if (!rows.length) {
    body = (
      <EmptyState>
        No devices yet.{isAdmin ? " Use “Add device” to add the first one." : ""}
      </EmptyState>
    );
  } else {
    body = (
      <div className="flex flex-col gap-4">
        {groupBySite(rows).map((group) => {
          const headingId = `site-${encodeURIComponent(group.site)}`;
          return (
            <section
              key={group.site}
              aria-labelledby={headingId}
              className="overflow-hidden rounded-lg border border-line bg-panel"
            >
              <div className="flex items-baseline justify-between gap-3 border-b border-line bg-inset px-4 py-2">
                <h2 id={headingId} className="truncate text-sm font-semibold">
                  {group.site}
                </h2>
                <Summary rows={group.rows} />
              </div>
              <div
                aria-hidden="true"
                className={`hidden border-b border-line py-1.5 text-xs text-muted md:grid ${isAdmin ? COLUMNS.admin : COLUMNS.viewer}`}
              >
                <span>Device</span>
                <span>State</span>
                <span>Last error</span>
                <span>Last success</span>
                {isAdmin && <span />}
              </div>
              <ul aria-label={`Devices, ${group.site}`} className="divide-y divide-line">
                {group.rows.map((row) => (
                  <DeviceRow
                    key={row.instance.id}
                    row={row}
                    typeName={typeName(row.instance.type_id)}
                    isAdmin={isAdmin}
                    now={now}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    );
  }

  return (
    <>
      <PageHeader title="Devices">
        <div className="flex items-center gap-4">
          {rows.length > 0 && <Summary rows={rows} />}
          {isAdmin && (
            <Link to="/devices/new" className={buttonPrimary}>
              Add device
            </Link>
          )}
        </div>
      </PageHeader>
      {body}
    </>
  );
}
