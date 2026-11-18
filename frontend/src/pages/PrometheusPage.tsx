// Settings -> Prometheus (admin only): the URL (server config, read-only), the mode, and
// the optional auth from the backend to Prometheus. The password or token is write-only:
// never pre-filled, and the page only knows whether one is set.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api";
import type { PrometheusAuthKind, PrometheusProbe, PrometheusSettings } from "../api";
import { useApiClient } from "../app/session";
import { systemAlertCause } from "../app/health";
import { PageHeader } from "../components/PageHeader";
import {
  alertError,
  alertInfo,
  alertSuccess,
  buttonPrimary,
  buttonSecondary,
  fieldHint,
  input,
  label,
} from "../components/styles";

const KEY = ["settings", "prometheus"] as const;

const KIND_LABELS: Record<PrometheusAuthKind, string> = {
  none: "None",
  basic: "Basic (user name and password)",
  bearer: "Bearer token",
};

function saveError(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) {
    return "Not saved: the master key doesn't match the stored secrets. Fix the key first.";
  }
  if (error instanceof ApiError && error.status === 422) {
    return "Not saved: enter the user name and the password or token.";
  }
  return "Could not save. Try again.";
}

function ProbeMessage({ probe }: { probe: PrometheusProbe }) {
  if (probe.ok) {
    return (
      <p role="status" className={alertSuccess}>
        Prometheus answered.
      </p>
    );
  }
  const cause = systemAlertCause("prometheus_unreachable", probe.reason);
  return (
    <p role="alert" className={alertError}>
      Prometheus did not answer. {cause}
    </p>
  );
}

function AuthForm({ data }: { data: PrometheusSettings }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<PrometheusAuthKind>(data.auth.kind);
  const [username, setUsername] = useState(data.auth.username ?? "");
  // Never pre-filled; empty on save means "unchanged".
  const [secret, setSecret] = useState("");

  const save = useMutation({
    mutationFn: () =>
      api.setPrometheusAuth({
        auth: {
          kind,
          ...(kind === "basic" ? { username } : {}),
          ...(kind !== "none" && secret ? { secret } : {}),
        },
      }),
    onSuccess: (result) => {
      setSecret("");
      queryClient.setQueryData(KEY, result);
    },
  });
  const probe = useMutation({ mutationFn: () => api.testPrometheus() });

  const sameKindStored = data.auth.kind === kind && data.auth.secret_set && !data.auth.unreadable;
  const secretLabel = kind === "basic" ? "Password" : "Token";
  const plainHttp = data.url.startsWith("http://");

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (save.isPending) return;
    probe.reset();
    save.mutate();
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      {data.auth.unreadable && (
        <p role="alert" className={alertError}>
          Stored credentials can&apos;t be read; save again or choose None.
        </p>
      )}
      <div>
        <label htmlFor="prom-kind" className={label}>
          Authentication
        </label>
        <select
          id="prom-kind"
          className={input}
          value={kind}
          onChange={(e) => setKind(e.target.value as PrometheusAuthKind)}
        >
          {(Object.keys(KIND_LABELS) as PrometheusAuthKind[]).map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k]}
            </option>
          ))}
        </select>
        <p className={fieldHint}>Used for every request Vantage sends to Prometheus.</p>
      </div>
      {kind === "basic" && (
        <div>
          <label htmlFor="prom-user" className={label}>
            User name
          </label>
          <input
            id="prom-user"
            className={input}
            autoComplete="off"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
      )}
      {kind !== "none" && (
        <div>
          <label htmlFor="prom-secret" className={label}>
            {secretLabel}
          </label>
          <input
            id="prom-secret"
            type="password"
            className={input}
            autoComplete="new-password"
            value={secret}
            placeholder={sameKindStored ? "Leave empty to keep the stored one" : ""}
            onChange={(e) => setSecret(e.target.value)}
          />
          <p className={fieldHint}>
            {secretLabel}:{" "}
            <span data-testid="secret-state">{sameKindStored ? "set" : "not set"}</span>. It is
            never shown again.
          </p>
        </div>
      )}
      {kind === "basic" && plainHttp && (
        <p role="note" className={alertInfo}>
          This Prometheus address uses plain http://, so the password travels unencrypted. Use
          https:// if you can.
        </p>
      )}
      {save.isError && (
        <p role="alert" className={alertError}>
          {saveError(save.error)}
        </p>
      )}
      {save.isSuccess && (
        <p role="status" className={alertSuccess}>
          Saved. It applies to the next request.
        </p>
      )}
      {probe.data && <ProbeMessage probe={probe.data} />}
      {probe.isError && (
        <p role="alert" className={alertError}>
          Could not run the test. Try again.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" className={buttonPrimary} disabled={save.isPending}>
          Save
        </button>
        <button
          type="button"
          className={buttonSecondary}
          disabled={probe.isPending}
          onClick={() => probe.mutate()}
        >
          Test
        </button>
      </div>
      <p className={fieldHint}>Test sends a harmless query with the saved settings.</p>
    </form>
  );
}

export function PrometheusPage() {
  const api = useApiClient();
  const settings = useQuery({ queryKey: KEY, queryFn: () => api.prometheusSettings() });
  return (
    <>
      <PageHeader title="Prometheus" />
      {settings.isError && (
        <p role="alert" className={alertError}>
          Could not load the settings.
        </p>
      )}
      {settings.data && (
        <section
          aria-labelledby="prom-title"
          className="max-w-lg rounded-lg border border-line bg-panel p-4"
        >
          <h2 id="prom-title" className="mb-1 text-base font-semibold">
            Connection to Prometheus
          </h2>
          <dl className="mb-4 space-y-1 text-sm">
            <div>
              <dt className="inline font-medium">Address: </dt>
              <dd className="inline break-all" data-testid="prom-url">
                {settings.data.url}
              </dd>
              <p className={fieldHint}>
                Set by <code>VANTAGE_PROMETHEUS_URL</code>; change it in the compose file.
              </p>
            </div>
            <div>
              <dt className="inline font-medium">Mode: </dt>
              <dd className="inline">{settings.data.mode}</dd>
            </div>
          </dl>
          <AuthForm
            // Saving replaces the data; remount so the form starts from what is stored.
            key={`${settings.data.auth.kind}:${settings.data.auth.username ?? ""}:${String(settings.data.auth.secret_set)}:${String(settings.data.auth.unreadable ?? false)}`}
            data={settings.data}
          />
        </section>
      )}
    </>
  );
}
