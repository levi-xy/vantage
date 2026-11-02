import type { Instance, InstanceStatus } from "../api";
import { shownState, useScrapeStates } from "./metrics/scrapeStatus";
import type { CardProps } from "./registry";

const STATE_LABELS: Record<InstanceStatus["state"], string> = {
  up: "UP",
  down: "DOWN",
  pending: "PENDING",
  unknown: "UNKNOWN",
};

/** The instance's status as plain text (red when down), with the collector's short error. */
export function StatusBadge({ instance }: { instance: Instance | undefined }) {
  const state = shownState(instance, useScrapeStates([instance]));
  return (
    <>
      <span
        data-state={state}
        className={`w-fit text-xs tracking-wide ${state === "down" ? "font-semibold text-crit-text" : "text-muted"}`}
      >
        <span className="sr-only">Status: </span>
        {STATE_LABELS[state]}
      </span>
      {instance?.status.error && (
        <span className="text-xs text-crit-text">{instance.status.error}</span>
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
