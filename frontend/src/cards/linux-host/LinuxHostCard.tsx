import type { CardProps } from "../registry";
import { cardFreshness } from "../metrics/freshness";
import { CardFrame, UsageBar } from "../metrics/CardFrame";
import { formatBytes, formatCelsius, formatPercent } from "../metrics/format";
import { byLabel, firstSeries, firstValue } from "../metrics/promql";
import { StatTile } from "../metrics/StatTile";
import { useInstantQuery, useRangeQuery } from "../metrics/useMetricQuery";
import { useUnits } from "../../app/units";
import { Figures, KeyFigure } from "../metrics/KeyFigure";
import { useCardTier } from "../metrics/tier";

// Scraped from node_exporter (native names). RAID, ZFS and sensors only exist on some hosts,
// so those sections appear only when their queries return series.
const REAL_FS = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|ramfs|fuse.*"';
const REAL_NIC = 'device!~"lo|veth.*|docker.*|br-.*|virbr.*"';

export function LinuxHostCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const cpuExpr = (sel: (m?: string) => string) =>
    `100 * (1 - avg(rate(node_cpu_seconds_total${sel('mode="idle"')}[5m])))`;
  const memExpr = (sel: (m?: string) => string) =>
    `100 * (1 - node_memory_MemAvailable_bytes${sel()} / node_memory_MemTotal_bytes${sel()})`;
  const rxExpr = (sel: (m?: string) => string) =>
    `8 * sum(rate(node_network_receive_bytes_total${sel(REAL_NIC)}[5m]))`;
  const txExpr = (sel: (m?: string) => string) =>
    `8 * sum(rate(node_network_transmit_bytes_total${sel(REAL_NIC)}[5m]))`;

  const cpu = useInstantQuery(instanceId, cpuExpr);
  const mem = useInstantQuery(instanceId, memExpr);
  const memTotal = useInstantQuery(instanceId, (sel) => `node_memory_MemTotal_bytes${sel()}`);
  const uptime = useInstantQuery(instanceId, (sel) => `time() - node_boot_time_seconds${sel()}`);
  const rx = useInstantQuery(instanceId, rxExpr);
  const tx = useInstantQuery(instanceId, txExpr);
  const disks = useInstantQuery(
    instanceId,
    (sel) =>
      `100 * (1 - node_filesystem_avail_bytes${sel(REAL_FS)} / node_filesystem_size_bytes${sel(REAL_FS)})`,
  );
  const raid = useInstantQuery(instanceId, (sel) => `node_md_degraded${sel()}`);
  const zfs = useInstantQuery(instanceId, (sel) => `node_zfs_zpool_state${sel()} == 1`);
  const temps = useInstantQuery(instanceId, (sel) => `node_hwmon_temp_celsius${sel()}`);

  const cpuSpark = useRangeQuery(instanceId, cpuExpr);
  const memSpark = useRangeQuery(instanceId, memExpr);
  const rxSpark = useRangeQuery(instanceId, rxExpr);
  const txSpark = useRangeQuery(instanceId, txExpr);

  const cpuValue = firstValue(cpu.data);
  const memValue = firstValue(mem.data);
  const uptimeValue = firstValue(uptime.data);
  const memTotalValue = firstValue(memTotal.data);

  // Several mounts can share one device (bind mounts): show each mountpoint once.
  const seen = new Set<string>();
  const volumes = byLabel(disks.data, "mountpoint")
    .filter((v) => v.value !== undefined && !seen.has(v.key) && seen.add(v.key))
    .sort((a, b) => a.key.localeCompare(b.key))
    .slice(0, 8);
  const arrays = byLabel(raid.data, "device");
  const pools = byLabel(zfs.data, "zpool");
  const hottest = temps.data?.series
    .map((s) => Number(s.values[0]?.[1]))
    .filter(Number.isFinite)
    .reduce<number | undefined>((max, v) => (max === undefined || v > max ? v : max), undefined);

  const tier = useCardTier();
  // The fullest volume stands for "disk" below full size.
  const fullest = volumes.reduce<number | undefined>(
    (max, v) => (max === undefined || (v.value ?? 0) > max ? v.value : max),
    undefined,
  );
  // A degraded array or pool is a problem: it replaces the figure below full size.
  const badArray = arrays.find((a) => a.value !== 0);
  const badPool = pools.find((p) => p.labels.state !== "online");
  const storageProblem = badArray
    ? `RAID ${badArray.key} degraded`
    : badPool
      ? `ZFS ${badPool.key} ${badPool.labels.state ?? "unknown"}`
      : undefined;

  return (
    <CardFrame
      title={title}
      showTitle={false}
      instance={instance}
      loading={cpu.isPending && mem.isPending}
      error={cpu.error ?? mem.error}
      freshness={cardFreshness(cpu, mem, memTotal, uptime, rx, tx, disks, raid, zfs, temps)}
      empty={cpuValue === undefined && memValue === undefined && uptimeValue === undefined}
      uptimeSeconds={uptimeValue}
    >
      {tier === "small" ? (
        storageProblem ? (
          <KeyFigure label="Storage health" tone="bad">
            {storageProblem}
          </KeyFigure>
        ) : (
          <KeyFigure label="CPU, memory and fullest disk">
            CPU {formatPercent(cpuValue)} · Mem {formatPercent(memValue)} · Disk{" "}
            {formatPercent(fullest)}
          </KeyFigure>
        )
      ) : tier === "medium" ? (
        <>
          <Figures>
            <StatTile label="CPU" value={formatPercent(cpuValue)} />
            <StatTile label="Memory" value={formatPercent(memValue)} />
            <StatTile
              label="Disk"
              value={formatPercent(fullest)}
              tone={fullest !== undefined && fullest >= 90 ? "bad" : undefined}
            />
          </Figures>
          {storageProblem && <p className="truncate text-sm text-crit-text">{storageProblem}</p>}
        </>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            <StatTile
              label="CPU"
              value={formatPercent(cpuValue)}
              spark={firstSeries(cpuSpark.data)}
            />
            <StatTile
              label={`Memory${memTotalValue ? ` of ${formatBytes(memTotalValue)}` : ""}`}
              value={formatPercent(memValue)}
              spark={firstSeries(memSpark.data)}
            />
            <StatTile
              label="Network in"
              value={formatRate(firstValue(rx.data))}
              spark={firstSeries(rxSpark.data)}
            />
            <StatTile
              label="Network out"
              value={formatRate(firstValue(tx.data))}
              spark={firstSeries(txSpark.data)}
            />
            {hottest !== undefined && (
              <StatTile label="Hottest sensor" value={formatCelsius(hottest)} />
            )}
          </div>

          {volumes.length > 0 && (
            <section aria-label="Disks" className="flex flex-col gap-1.5">
              {volumes.map((v) => (
                <UsageBar
                  key={v.key}
                  label={v.key}
                  percent={v.value ?? 0}
                  text={formatPercent(v.value)}
                />
              ))}
            </section>
          )}

          {(arrays.length > 0 || pools.length > 0) && (
            <section aria-label="Storage health" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {arrays.map((a) => {
                const ok = a.value === 0;
                return (
                  <span key={`md-${a.key}`}>
                    RAID {a.key}:{" "}
                    <span className={ok ? "" : "text-crit-text"}>
                      {ok ? "healthy" : "degraded"}
                    </span>
                  </span>
                );
              })}
              {pools.map((p) => {
                const state = p.labels.state ?? "unknown";
                return (
                  <span key={`zfs-${p.key}`}>
                    ZFS {p.key}:{" "}
                    <span className={state === "online" ? "" : "text-crit-text"}>{state}</span>
                  </span>
                );
              })}
            </section>
          )}
        </>
      )}
    </CardFrame>
  );
}
