import { useState } from "react";
import { Link, useParams } from "react-router";
import type { Instance, IntegrationInfo } from "../../api";
import { DeviceTypeIcon } from "../../cards/metrics/CardHeader";
import { shownState, useScrapeStates } from "../../cards/metrics/scrapeStatus";
import { EmptyState, PageHeader } from "../../components/PageHeader";
import { alertError, buttonSecondary } from "../../components/styles";
import { deviceHealth, TONE_CLASS } from "./deviceStatus";
import { InstanceForm } from "./InstanceForm";
import { errorText, useInstance, useIntegrations } from "./queries";
import { StateText } from "./StateText";

const backLink = (
  <Link to="/devices" className={buttonSecondary}>
    Back to devices
  </Link>
);

function Loading() {
  return (
    <p role="status" className="text-sm text-muted">
      Loading…
    </p>
  );
}

function LoadError({ error }: { error: unknown }) {
  return (
    <p role="alert" className={alertError}>
      {errorText(error)}
    </p>
  );
}

/** Step 1 of "Add device": the integration types, grouped by category. */
function TypePicker({
  integrations,
  onPick,
}: {
  integrations: IntegrationInfo[];
  onPick: (type: IntegrationInfo) => void;
}) {
  if (!integrations.length) return <EmptyState>No integrations are available.</EmptyState>;
  const sorted = [...integrations].sort(
    (a, b) => a.category.localeCompare(b.category) || a.name.localeCompare(b.name),
  );
  return (
    <ul aria-label="Device types" className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {sorted.map((type) => (
        <li key={type.type_id}>
          <button
            type="button"
            onClick={() => onPick(type)}
            className="flex w-full items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2.5 text-left hover:border-in focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
          >
            <DeviceTypeIcon typeId={type.type_id} />
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-semibold">{type.name}</span>
              <span className="block text-xs text-muted capitalize">{type.category}</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** The edit page's line under the title: type, site and the current state. */
function DeviceSummary({ instance, typeName }: { instance: Instance; typeName: string }) {
  const scrape = useScrapeStates([instance]);
  const health = deviceHealth(instance, shownState(instance, scrape));
  return (
    <div className="mb-4 flex max-w-2xl min-w-0 items-center gap-3 rounded-lg border border-line bg-panel px-4 py-2.5">
      <DeviceTypeIcon typeId={instance.type_id} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs text-muted">
          {typeName} · {instance.site ?? "No site"}
        </div>
        {health.problem && (
          <div
            title={health.problem}
            className={`truncate text-xs ${health.problemTone ? TONE_CLASS[health.problemTone] : "text-muted"}`}
          >
            {health.problem}
          </div>
        )}
      </div>
      <StateText health={health} />
    </div>
  );
}

/** "Add device": pick a type, then fill the form generated from its schema. */
export function AddDevicePage() {
  const integrations = useIntegrations();
  const [type, setType] = useState<IntegrationInfo | null>(null);

  if (type) {
    return (
      <>
        <PageHeader title={`Add ${type.name}`}>
          <button type="button" onClick={() => setType(null)} className={buttonSecondary}>
            Change type
          </button>
        </PageHeader>
        <InstanceForm key={type.type_id} integration={type} />
      </>
    );
  }
  return (
    <>
      <PageHeader title="Add device">{backLink}</PageHeader>
      {integrations.isPending ? (
        <Loading />
      ) : integrations.isError ? (
        <LoadError error={integrations.error} />
      ) : (
        <>
          <p className="mb-3 text-sm text-muted">What kind of device is it?</p>
          <TypePicker integrations={integrations.data} onPick={setType} />
        </>
      )}
    </>
  );
}

export function EditDevicePage() {
  const { id = "" } = useParams();
  const instance = useInstance(id);
  const integrations = useIntegrations();

  let body;
  if (instance.isPending || integrations.isPending) body = <Loading />;
  else if (instance.isError) body = <LoadError error={instance.error} />;
  else if (integrations.isError) body = <LoadError error={integrations.error} />;
  else {
    const type = integrations.data.find((t) => t.type_id === instance.data.type_id);
    body = type ? (
      <>
        <DeviceSummary instance={instance.data} typeName={type.name} />
        <InstanceForm key={instance.data.id} integration={type} instance={instance.data} />
      </>
    ) : (
      <p role="alert" className={alertError}>
        This device's integration ({instance.data.type_id}) is no longer available.
      </p>
    );
  }
  return (
    <>
      <PageHeader title={instance.data ? `Edit ${instance.data.name}` : "Edit device"}>
        {backLink}
      </PageHeader>
      {body}
    </>
  );
}
