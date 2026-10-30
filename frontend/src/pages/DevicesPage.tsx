import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import type { Instance, InstanceStatus, TestResult } from "../api";
import { useApiClient, useUser } from "../app/session";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError, buttonPrimary, buttonSecondary } from "../components/styles";
import { TestOutcome } from "./devices/InstanceForm";
import { shownState, useScrapeStates } from "../cards/metrics/scrapeStatus";
import { errorText, INSTANCES_KEY, useInstances, useIntegrations } from "./devices/queries";

const STATE_LABELS: Record<InstanceStatus["state"], string> = {
  up: "Up",
  down: "Down",
  pending: "Pending",
  unknown: "Unknown",
};

const STATE_STYLES: Record<InstanceStatus["state"], string> = {
  up: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  down: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  pending: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  unknown: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const badge = "inline-flex rounded px-2 py-0.5 text-xs font-medium";

function formatTime(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

function StatusBadge({ status }: { status: InstanceStatus }) {
  const checked = formatTime(status.checked_at);
  return (
    <span className="flex flex-col gap-0.5">
      <span className={`${badge} w-fit ${STATE_STYLES[status.state]}`}>
        <span className="sr-only">Status: </span>
        {STATE_LABELS[status.state]}
      </span>
      {status.error && (
        <span className="text-xs text-red-700 dark:text-red-400">{status.error}</span>
      )}
      {checked && (
        <span className="text-xs text-slate-500 dark:text-slate-400">Checked {checked}</span>
      )}
    </span>
  );
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
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {confirming ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(remove)}
              className={`${buttonSecondary} border-red-400 text-red-700 dark:border-red-700 dark:text-red-300`}
              aria-label={`Confirm deleting ${instance.name}`}
            >
              Confirm delete
            </button>
            <button type="button" onClick={() => setConfirming(false)} className={buttonSecondary}>
              Keep
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(test)}
              className={buttonSecondary}
              aria-label={`Test ${instance.name}`}
            >
              {test.isPending ? "Testing…" : "Test"}
            </button>
            <Link
              to={`/devices/${encodeURIComponent(instance.id)}`}
              className={buttonSecondary}
              aria-label={`Edit ${instance.name}`}
            >
              Edit
            </Link>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(toggle)}
              className={buttonSecondary}
              aria-label={`${instance.enabled ? "Disable" : "Enable"} ${instance.name}`}
            >
              {instance.enabled ? "Disable" : "Enable"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(true)}
              className={buttonSecondary}
              aria-label={`Delete ${instance.name}`}
            >
              Delete
            </button>
          </>
        )}
      </div>
      {error && (
        <p role="alert" className={alertError}>
          {error}
        </p>
      )}
      {result && <TestOutcome result={result} />}
    </div>
  );
}

/** The list of devices (instances). Admins can add, test, edit, disable and delete them. */
export function DevicesPage() {
  const user = useUser();
  const isAdmin = user.role === "admin";
  const instances = useInstances();
  const scrape = useScrapeStates(instances.data ?? []);
  const integrations = useIntegrations();
  const typeName = (typeId: string) =>
    integrations.data?.find((t) => t.type_id === typeId)?.name ?? typeId;

  let body;
  if (instances.isPending) {
    body = (
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading…
      </p>
    );
  } else if (instances.isError) {
    body = (
      <p role="alert" className={alertError}>
        {errorText(instances.error)}
      </p>
    );
  } else if (!instances.data.length) {
    body = (
      <EmptyState>
        No devices yet.{isAdmin ? " Use “Add device” to add the first one." : ""}
      </EmptyState>
    );
  } else {
    body = (
      <ul aria-label="Devices" className="space-y-3">
        {instances.data.map((instance) => (
          <li
            key={instance.id}
            className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4 md:flex-row md:items-start md:justify-between dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex min-w-0 flex-col gap-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-medium break-all">{instance.name}</span>
                {!instance.enabled && (
                  <span className={`${badge} ${STATE_STYLES.unknown}`}>Disabled</span>
                )}
              </span>
              <span className="text-sm text-slate-600 dark:text-slate-400">
                {typeName(instance.type_id)} · {instance.site ?? "No site"}
              </span>
              {instance.tags.length > 0 && (
                <span className="flex flex-wrap gap-1">
                  {instance.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800"
                    >
                      {tag}
                    </span>
                  ))}
                </span>
              )}
            </div>
            <div className="flex flex-col gap-3 md:items-end">
              <StatusBadge status={{ ...instance.status, state: shownState(instance, scrape) }} />
              {isAdmin && <AdminActions instance={instance} />}
            </div>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <>
      <PageHeader title="Devices">
        {isAdmin && (
          <Link to="/devices/new" className={buttonPrimary}>
            Add device
          </Link>
        )}
      </PageHeader>
      {body}
    </>
  );
}
