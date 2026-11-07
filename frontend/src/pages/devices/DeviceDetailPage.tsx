// A device's own page, read-only for every role: its state in full, its open alerts and all
// of its cards, larger than on the dashboard. Data only through the typed API client.

import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { Link, useLocation, useParams } from "react-router";
import { ApiError } from "../../api";
import type { AlertEvent, CardSpec, Instance } from "../../api";
import { deviceColor } from "../../app/deviceColors";
import { useApiClient, useUser } from "../../app/session";
import { DeviceColorIcon, deviceTypeLabel } from "../../cards/metrics/CardHeader";
import { shownState, useScrapeStates } from "../../cards/metrics/scrapeStatus";
import { DESKTOP_MIN_WIDTH, gridHeightPx } from "../../cards/layout";
import { ageText } from "../../cards/problems";
import { useOpenAlerts } from "../../cards/openAlerts";
import { cardComponent, fullSize } from "../../cards/registry";
import { buttonSecondary } from "../../components/styles";
import { alertError } from "../../components/styles";
import { panel, row, toneText, useNow } from "../alerts/tone";
import { deviceHealth, timeText } from "./deviceStatus";
import { errorText, useInstance } from "./queries";
import { DeviceHistory } from "./history/DeviceHistory";
import { StateText } from "./StateText";

function subscribeResize(onChange: () => void) {
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}

function useIsDesktop() {
  return useSyncExternalStore(subscribeResize, () => window.innerWidth >= DESKTOP_MIN_WIDTH);
}

function BackLink() {
  const location = useLocation();
  const from = (location.state as { from?: unknown } | null)?.from;
  const to = typeof from === "string" && from.startsWith("/") ? from : "/";
  return (
    <Link to={to} className={buttonSecondary}>
      {to === "/" ? "Back to the dashboard" : "Back"}
    </Link>
  );
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="text-sm break-words">{children}</dd>
    </div>
  );
}

function Header({ instance }: { instance: Instance }) {
  const isAdmin = useUser().role === "admin";
  const now = useNow();
  const scrape = useScrapeStates([instance]);
  const health = deviceHealth(instance, shownState(instance, scrape));
  const { status } = instance;
  const checked = timeText(status.checked_at, now);
  const last = timeText(status.last_success_at, now);
  const color = deviceColor(instance);
  return (
    <section aria-label="Device" className={`${panel} mb-4 p-4`}>
      <div className="flex flex-wrap items-center gap-3">
        <DeviceColorIcon typeId={instance.type_id} color={color} />
        <div className="min-w-0 flex-1">
          <h1 className="flex min-w-0 items-center gap-2 text-xl font-semibold tracking-tight">
            {color && (
              <span
                role="img"
                aria-label={`Colour: ${instance.color}`}
                style={{ backgroundColor: color }}
                className="h-2.5 w-2.5 shrink-0 rounded-full"
              />
            )}
            <span className="min-w-0 break-words">{instance.name}</span>
          </h1>
          <p className="text-xs text-muted">
            {deviceTypeLabel(instance.type_id)} · {instance.site || "No site"}
          </p>
        </div>
        <StateText health={health} />
        {isAdmin && (
          <Link to={`/devices/${encodeURIComponent(instance.id)}/edit`} className={buttonSecondary}>
            Edit
          </Link>
        )}
      </div>
      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        <Detail label="Last checked">{checked ? checked.short : "not yet"}</Detail>
        <Detail label="Last success">
          {last ? last.short : health.scraped ? "scraped by Prometheus" : "never"}
        </Detail>
        {(health.problem || status.error) && (
          <div className="min-w-0 sm:col-span-2">
            <dt className="text-xs text-muted">Error</dt>
            <dd
              className={`text-sm break-words ${health.problemTone ? toneText(health.problemTone === "crit" ? "crit" : "warn") : ""}`}
            >
              {status.error ?? health.problem}
            </dd>
          </div>
        )}
        {instance.tags.length > 0 && (
          <div className="min-w-0 sm:col-span-2">
            <dt className="sr-only">Tags</dt>
            <dd>
              <ul aria-label="Tags" className="flex flex-wrap gap-1">
                {instance.tags.map((t) => (
                  <li key={t} className="rounded bg-chip px-1.5 py-px text-[11px] text-muted">
                    {t}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}

function OpenAlerts({ instanceId }: { instanceId: string }) {
  const now = useNow();
  // The query the dashboard shares: kept fresh by /ws pushes and a refetch interval.
  const alerts = useOpenAlerts();
  const mine: AlertEvent[] = (alerts.data ?? []).filter((e) => e.instance_id === instanceId);
  return (
    <section aria-label="Open alerts" className="mb-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">Open alerts</h2>
        <Link to="/alerts" className="text-sm text-in hover:underline">
          All alerts
        </Link>
      </div>
      {alerts.isError ? (
        <p role="alert" className={alertError}>
          {errorText(alerts.error)}
        </p>
      ) : alerts.isPending ? (
        <p role="status" className="text-sm text-muted">
          Loading…
        </p>
      ) : mine.length === 0 ? (
        <p className="text-sm text-muted">No open alerts</p>
      ) : (
        <ul className={panel}>
          {mine.map((e) => {
            const since = Date.parse(e.state_since ?? e.started_at);
            const tone =
              e.state === "no_data" || e.severity === "warning"
                ? "warn"
                : e.severity === "critical"
                  ? "crit"
                  : "plain";
            return (
              <li key={e.id} className={`${row} flex flex-wrap items-baseline gap-x-3 gap-y-0.5`}>
                <span className="min-w-0 font-medium break-words">{e.rule_name}</span>
                <span className={`text-xs ${toneText(tone)}`}>
                  {e.state === "no_data" ? "NO DATA" : "FIRING"} · {e.severity}
                </span>
                <span className="text-xs text-muted">
                  since {Number.isNaN(since) ? "—" : `${ageText(now - since)} ago`}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Cards({ instance, desktop }: { instance: Instance; desktop: boolean }) {
  const api = useApiClient();
  const cards = useQuery({
    queryKey: [...["instances"], instance.id, "cards"],
    queryFn: () => api.instanceCards(instance.id),
  });
  const color = deviceColor(instance);
  if (cards.isError)
    return (
      <p role="alert" className={alertError}>
        {errorText(cards.error)}
      </p>
    );
  if (cards.isPending)
    return (
      <p role="status" className="text-sm text-muted">
        Loading…
      </p>
    );
  if (cards.data.length === 0)
    return <p className="text-sm text-muted">This device has no cards.</p>;
  return (
    <section aria-label="Cards">
      <h2 className="mb-2 text-base font-semibold">Cards</h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {cards.data.map((spec: CardSpec) => {
          const Card = cardComponent(spec.card_type);
          // Comfortable height on desktop: the card's full size plus a row; on a phone each
          // card is as tall as its content.
          const height = desktop ? gridHeightPx(fullSize(spec.card_type)[1] + 1) : undefined;
          return (
            <div
              key={spec.card_type}
              data-testid="detail-card"
              style={
                {
                  ...(height ? { height } : {}),
                  ...(color ? { "--vt-device": color } : {}),
                } as React.CSSProperties
              }
              className="relative min-w-0 overflow-hidden rounded-xl border border-line bg-panel"
            >
              <Card
                instanceId={instance.id}
                settings={{}}
                title={spec.title}
                instance={instance}
                compact={!desktop}
              />
              {color && (
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-0 rounded-[11px] border-t-[3px] border-device"
                />
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function DeviceDetailPage() {
  const { id = "" } = useParams();
  const instance = useInstance(id);
  const desktop = useIsDesktop();

  if (instance.isPending)
    return (
      <p role="status" className="text-sm text-muted">
        Loading…
      </p>
    );
  if (instance.isError) {
    const missing = instance.error instanceof ApiError && instance.error.status === 404;
    return (
      <>
        <h1 className="mb-4 text-xl font-semibold tracking-tight">
          {missing ? "Device not found" : "Device"}
        </h1>
        {!missing && (
          <p role="alert" className={`${alertError} mb-3`}>
            {errorText(instance.error)}
          </p>
        )}
        <BackLink />
      </>
    );
  }
  return (
    <>
      <div className="mb-3">
        <BackLink />
      </div>
      <Header instance={instance.data} />
      <OpenAlerts instanceId={instance.data.id} />
      <DeviceHistory instance={instance.data} />
      <Cards instance={instance.data} desktop={desktop} />
    </>
  );
}
