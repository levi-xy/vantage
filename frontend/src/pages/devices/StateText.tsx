import type { DeviceHealth } from "./deviceStatus";
import { TONE_CLASS } from "./deviceStatus";

/** The state word, with "Status: " for screen readers. */
export function StateText({ health, title }: { health: DeviceHealth; title?: string }) {
  return (
    <span
      title={title}
      className={`text-xs tracking-wide whitespace-nowrap ${health.tone ? TONE_CLASS[health.tone] : "text-muted"}`}
    >
      <span className="sr-only">Status: </span>
      {health.text}
    </span>
  );
}
