import type { CardProps } from "../registry";
import { CardFrame } from "../metrics/CardFrame";
import { formatCelsius, formatDuration } from "../metrics/format";
import { byLabel } from "../metrics/promql";
import { useInstantQuery } from "../metrics/useMetricQuery";

// Scraped from smartctl_exporter (native names), per disk (label `device`).
const INFO = "smartctl_device";
const STATUS = "smartctl_device_smart_status"; // 1 = overall health passed
const TEMPERATURE = "smartctl_device_temperature";
const POWER_ON = "smartctl_device_power_on_seconds"; // not on every drive or exporter version

export function SmartDisksCard({ instanceId, title, instance }: CardProps) {
  const info = useInstantQuery(instanceId, (sel) => `${INFO}${sel()}`);
  const status = useInstantQuery(instanceId, (sel) => `${STATUS}${sel()}`);
  const temperature = useInstantQuery(
    instanceId,
    (sel) =>
      // The current temperature; some drives also report limits under other types.
      `${TEMPERATURE}${sel('temperature_type="current"')}`,
  );
  const powerOn = useInstantQuery(instanceId, (sel) => `${POWER_ON}${sel()}`);

  const statuses = byLabel(status.data, "device");
  const model = new Map(byLabel(info.data, "device").map((r) => [r.key, r.labels.model_name]));
  const temp = new Map(byLabel(temperature.data, "device").map((r) => [r.key, r.value]));
  const hours = new Map(byLabel(powerOn.data, "device").map((r) => [r.key, r.value]));

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={status.isPending}
      error={status.error ?? temperature.error}
      empty={statuses.length === 0}
      emptyHint="No disks reported. Check that smartctl_exporter can read the disks."
    >
      <ul className="flex flex-col gap-2">
        {[...statuses]
          .sort((a, b) => a.key.localeCompare(b.key))
          .map((disk) => {
            const healthy = disk.value === 1;
            const poh = hours.get(disk.key);
            return (
              <li key={disk.key} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-sm">
                <span className="font-medium">{disk.key}</span>
                <span
                  className={
                    healthy
                      ? "text-emerald-700 dark:text-emerald-400"
                      : "text-red-700 dark:text-red-400"
                  }
                >
                  {healthy ? "Healthy" : "Failing"}
                </span>
                <span className="tabular-nums">{formatCelsius(temp.get(disk.key))}</span>
                {poh !== undefined && (
                  <span className="text-slate-500 tabular-nums dark:text-slate-400">
                    {Math.round(poh / 3600).toLocaleString("en")} h powered on
                    <span className="sr-only"> ({formatDuration(poh)})</span>
                  </span>
                )}
                {model.get(disk.key) && (
                  <span className="w-full truncate text-xs text-slate-500 dark:text-slate-400">
                    {model.get(disk.key)}
                  </span>
                )}
              </li>
            );
          })}
      </ul>
    </CardFrame>
  );
}
