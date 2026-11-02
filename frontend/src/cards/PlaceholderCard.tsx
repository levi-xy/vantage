import { CardHeader } from "./metrics/CardHeader";
import { FRESH } from "./metrics/freshness";
import type { CardProps } from "./registry";

/** Shown for card types that have no component yet: the shared header and the title. */
export function PlaceholderCard({ title, instance }: CardProps) {
  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-hidden p-4">
      <CardHeader instance={instance} freshness={FRESH} />
      <span className="text-xs font-medium text-muted">{title}</span>
    </div>
  );
}
