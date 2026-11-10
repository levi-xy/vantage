import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame } from "../metrics/CardFrame";
import { formatPercent } from "../metrics/format";
import { byLabel, firstValue } from "../metrics/promql";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { StatTile } from "../metrics/StatTile";
import { useCardTier } from "../metrics/tier";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Polled by Vantage (integrations/builtin/opnsense): vantage_opnsense_*.
const P = "vantage_opnsense_";
type Sel = (extra?: string) => string;

const load = (n: number) => (v: number | undefined) => (v === undefined ? "–" : v.toFixed(n));

/** Load, memory, firewall states and firmware version of one OPNsense firewall. */
export function OpnsenseOverviewCard({ instanceId, title, instance }: CardProps) {
  const tier = useCardTier();
  const load1 = useInstantQuery(instanceId, (sel: Sel) => `${P}load1${sel()}`);
  const load5 = useInstantQuery(instanceId, (sel: Sel) => `${P}load5${sel()}`, {
    enabled: tier === "full",
  });
  const mem = useInstantQuery(
    instanceId,
    (sel: Sel) => `100 * ${P}memory_used_bytes${sel()} / ${P}memory_total_bytes${sel()}`,
  );
  const states = useInstantQuery(instanceId, (sel: Sel) => `${P}pf_states${sel()}`);
  const uptime = useInstantQuery(instanceId, (sel: Sel) => `${P}uptime_seconds${sel()}`);
  const info = useInstantQuery(instanceId, (sel: Sel) => `${P}info${sel()}`);

  const loadValue = firstValue(load1.data);
  const memValue = firstValue(mem.data);
  const statesValue = firstValue(states.data);
  const version = byLabel(info.data, "version")[0]?.key;

  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={load1.isPending && mem.isPending}
      error={load1.error ?? mem.error}
      freshness={cardFreshness(load1, mem, states, uptime, info)}
      empty={loadValue === undefined && memValue === undefined && statesValue === undefined}
      uptimeSeconds={firstValue(uptime.data)}
    >
      {tier === "small" ? (
        <KeyFigure label="Load">Load {load(2)(loadValue)}</KeyFigure>
      ) : tier === "medium" ? (
        <Figures>
          <StatTile label="Load" value={load(2)(loadValue)} />
          <StatTile label="Memory" value={formatPercent(memValue)} />
        </Figures>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <StatTile label="Load (1 min)" value={load(2)(loadValue)} />
          <StatTile label="Load (5 min)" value={load(2)(firstValue(load5.data))} />
          <StatTile label="Memory" value={formatPercent(memValue)} />
          <StatTile
            label="States"
            value={statesValue === undefined ? "–" : String(Math.round(statesValue))}
          />
          <StatTile label="Firmware" value={version ? version.replace(/^OPNsense\s+/, "") : "–"} />
        </div>
      )}
    </CardFrame>
  );
}
