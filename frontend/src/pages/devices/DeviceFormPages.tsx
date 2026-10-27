import { useState } from "react";
import { Link, useParams } from "react-router";
import type { IntegrationInfo } from "../../api";
import { EmptyState, PageHeader } from "../../components/PageHeader";
import { alertError, buttonSecondary } from "../../components/styles";
import { InstanceForm } from "./InstanceForm";
import { errorText, useInstance, useIntegrations } from "./queries";

const backLink = (
  <Link to="/devices" className={buttonSecondary}>
    Back to devices
  </Link>
);

function Loading() {
  return (
    <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
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
    <ul aria-label="Device types" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {sorted.map((type) => (
        <li key={type.type_id}>
          <button
            type="button"
            onClick={() => onPick(type)}
            className="w-full rounded-lg border border-slate-200 bg-white p-4 text-left hover:border-sky-500 focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:outline-none dark:border-slate-800 dark:bg-slate-900"
          >
            <span className="block font-medium">{type.name}</span>
            <span className="block text-xs text-slate-500 capitalize dark:text-slate-400">
              {type.category}
            </span>
          </button>
        </li>
      ))}
    </ul>
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
          <p className="mb-3 text-sm text-slate-600 dark:text-slate-400">
            What kind of device is it?
          </p>
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
      <InstanceForm key={instance.data.id} integration={type} instance={instance.data} />
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
