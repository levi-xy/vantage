import { useUnits } from "../../app/units";
import type { CardProps, CardSettingsProps } from "../registry";
import { cardFreshness, FRESH, NO_RECENT_DATA } from "../metrics/freshness";
import type { Freshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";
import type { InstantQuery } from "../metrics/useMetricQuery";
import { KeyFigure } from "../metrics/KeyFigure";
import { useCardTier, useFittingRows } from "../metrics/tier";
import {
  IF_INFO_METRIC,
  interfaceDescriptions,
  interfaceSettings,
  REAL_IF,
} from "../pfsense-interfaces/interfaceSettings";
import {
  gatewayRows,
  interfacesFromSettings,
  MAX_INTERFACES,
  problemsFirst,
  settingsWithInterfaces,
} from "./picked";

/** The interfaces' raw counters: one that drops out of the latest scrape is gone at once. */
function usePresent(instanceId: string): InstantQuery {
  return useInstantQuery(instanceId, (sel) => `net_bytes_recv${sel(REAL_IF)}`);
}

/** The interfaces' display names: device settings, then pfSense's description, then the OS name. */
function useInterfaceNames(instanceId: string, instance: CardProps["instance"]) {
  const info = useInstantQuery(instanceId, (sel) => `${IF_INFO_METRIC}${sel()}`);
  return interfaceSettings(instance, interfaceDescriptions(info.data));
}

/**
 * The pfSense interfaces the admin picked in the card's settings, with their in/out rate.
 * Telegraf's `net` input carries no link state, so an interface whose counters are missing
 * from the latest scrape (or whose device's data is stale) shows NO DATA. Healthy rows are
 * plain muted text.
 */
export function PfsenseGatewaysCard({ instanceId, title, instance, settings }: CardProps) {
  const { formatRate } = useUnits();
  const tier = useCardTier();
  const chosen = interfacesFromSettings(settings);
  const present = usePresent(instanceId);
  // Same query text as the Interfaces card, so the two share one cached result.
  const rx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_recv${sel(REAL_IF)}[5m])`);
  const tx = useInstantQuery(instanceId, (sel) => `8 * rate(net_bytes_sent${sel(REAL_IF)}[5m])`);
  const names = useInterfaceNames(instanceId, instance);

  const freshness = cardFreshness(present, rx, tx);
  const stale = freshness.kind !== "fresh";
  const rows = gatewayRows(chosen, present.data, rx.data, tx.data, names.label, stale);
  // Past rates are never shown: every row says NO DATA instead, so the body isn't greyed
  // (a greyed NO DATA would read as healthy); the header still says the device has no data.
  const frameFreshness: Freshness =
    chosen.length === 0
      ? FRESH
      : freshness.kind === "stale"
        ? { kind: "missing", note: NO_RECENT_DATA }
        : freshness;

  const fitting = useFittingRows(28, rows.length);
  // Below full size problems come first, and when not all fit the last row says how many more.
  const ordered = tier === "full" ? rows : problemsFirst(rows);
  const shown =
    tier === "full"
      ? rows
      : ordered.slice(0, fitting < rows.length ? Math.max(1, fitting - 1) : fitting);
  // Small: the first picked interface, or the first one with a problem.
  const first = rows.find((r) => r.noData) ?? rows[0];

  let body;
  if (chosen.length === 0) {
    body =
      tier === "small" ? (
        <p className="truncate text-xs text-muted">No interfaces chosen.</p>
      ) : (
        <div className="flex flex-col gap-1">
          <p className="text-sm">No interfaces chosen.</p>
          <p className="text-xs text-muted">Edit dashboard, then ⚙ on this card.</p>
        </div>
      );
  } else if (tier === "small" && first) {
    body = first.noData ? (
      <KeyFigure label={`${first.name}: no data`} tone="bad">
        {first.name} · NO DATA
      </KeyFigure>
    ) : (
      <KeyFigure label={`${first.name}: in and out`}>
        <span className="text-in">↓</span>
        {formatRate(first.rx)} <span className="text-out">↑</span>
        {formatRate(first.tx)}
      </KeyFigure>
    );
  } else {
    body = (
      <ul aria-label="Gateways" className="flex flex-col gap-2 text-sm">
        {shown.map((r) => (
          <li key={r.id} className="flex min-w-0 items-baseline gap-2">
            <span className="truncate">{r.name}</span>
            {r.name !== r.id && <span className="truncate text-[11px] text-muted">{r.id}</span>}
            {r.noData ? (
              <span className="ml-auto shrink-0 text-xs font-semibold tracking-wide text-crit-text">
                NO DATA
              </span>
            ) : (
              <span className="ml-auto shrink-0 text-[13px] whitespace-nowrap text-muted tabular-nums">
                <span className="text-in">↓</span>
                {formatRate(r.rx)} <span className="text-out">↑</span>
                {formatRate(r.tx)}
              </span>
            )}
          </li>
        ))}
        {shown.length < rows.length && (
          <li className="text-xs text-muted">{rows.length - shown.length} more</li>
        )}
      </ul>
    );
  }

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={chosen.length > 0 && present.isPending}
      error={chosen.length > 0 ? (present.error ?? rx.error ?? tx.error) : null}
      freshness={frameFreshness}
      empty={false}
    >
      {body}
    </CardFrame>
  );
}

/** The interface picker, shown in edit mode (⚙ on the card). */
export function PfsenseGatewaysSettings({
  instanceId,
  instance,
  settings,
  onChange,
}: CardSettingsProps) {
  const present = usePresent(instanceId);
  const names = useInterfaceNames(instanceId, instance);
  const chosen = interfacesFromSettings(settings);
  const full = chosen.length >= MAX_INTERFACES;
  // Hidden interfaces aren't offered; a chosen one that stopped reporting stays, to untick.
  const reported = byLabel(present.data, "interface")
    .map((r) => r.key)
    .filter((id) => id !== "" && !names.hidden(id));
  const ids = [...new Set([...reported, ...chosen])].sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true }),
  );

  if (present.isPending && chosen.length === 0)
    return <p className="text-sm text-muted">Loading interfaces…</p>;
  if (ids.length === 0)
    return <p className="text-sm text-muted">This pfSense reports no interface counters yet.</p>;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        The card shows the traffic of the interfaces you tick (up to {MAX_INTERFACES}). Name them in
        the device settings or on pfSense.
      </p>
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs tracking-wide text-muted uppercase">
          {instance?.name ?? "Interfaces"}
        </legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {ids.map((id) => {
            const checked = chosen.includes(id);
            const name = names.label(id);
            return (
              <label key={id} className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!checked && full}
                  onChange={() =>
                    onChange(
                      settingsWithInterfaces(
                        settings,
                        checked ? chosen.filter((c) => c !== id) : [...chosen, id],
                      ),
                    )
                  }
                />
                {name}
                {name !== id && <span className="text-xs text-muted">({id})</span>}
              </label>
            );
          })}
        </div>
      </fieldset>
    </div>
  );
}
