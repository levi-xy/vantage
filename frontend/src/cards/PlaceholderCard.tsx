import type { Instance, InstanceStatus } from "../api";
import { shownState, useScrapeStates } from "./metrics/scrapeStatus";
import type { CardProps } from "./registry";

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

/** The instance's status as a coloured badge, with the collector's short error if any. */
export function StatusBadge({ instance }: { instance: Instance | undefined }) {
  const state = shownState(instance, useScrapeStates([instance]));
  return (
    <>
      <span
        className={`inline-flex w-fit rounded px-2 py-0.5 text-xs font-medium ${STATE_STYLES[state]}`}
      >
        <span className="sr-only">Status: </span>
        {STATE_LABELS[state]}
      </span>
      {instance?.status.error && (
        <span className="text-xs text-red-700 dark:text-red-400">{instance.status.error}</span>
      )}
    </>
  );
}

/** Shown for card types that have no component yet: the instance, its status, the title. */
export function PlaceholderCard({ title, instance }: CardProps) {
  return (
    <div className="flex h-full flex-col gap-1 overflow-hidden p-4">
      <span className="text-xs tracking-wide text-slate-500 uppercase dark:text-slate-400">
        {title}
      </span>
      <span className="font-medium break-all">{instance?.name ?? "Unknown device"}</span>
      <StatusBadge instance={instance} />
    </div>
  );
}
