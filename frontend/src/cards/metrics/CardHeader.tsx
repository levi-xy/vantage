// The header every device card shares: a type icon, the device name, a subtitle
// ("pfSense · up 7h 50m" or "pfSense · no data since 14:02") and a plain-text state on the
// right. Healthy is muted text; only a problem uses the critical colour. The collector's
// full error is never printed here (it would overflow small cards): it is the state's
// tooltip, and the health popover and the Devices page show it in full.

import { useEffect, useState } from "react";
import type { Instance } from "../../api";
import { shownState, useScrapeStates } from "./scrapeStatus";
import { formatAsOf } from "./freshness";
import type { Freshness } from "./freshness";
import { formatDuration } from "./format";

type Icon =
  "shield" | "router" | "switch" | "pulse" | "globe" | "server" | "disk" | "cloud" | "play";

/** A short type name and an icon per integration (`type_id`). Unknown types: generic. */
const DEVICE_TYPES: Readonly<Record<string, { label: string; icon: Icon }>> = {
  pfsense: { label: "pfSense", icon: "shield" },
  snmp_device: { label: "SNMP", icon: "router" },
  mikrotik_swos: { label: "MikroTik SwOS", icon: "switch" },
  ping_check: { label: "Ping", icon: "pulse" },
  http_check: { label: "HTTP check", icon: "globe" },
  linux_host: { label: "Linux host", icon: "server" },
  proxmox: { label: "Proxmox VE", icon: "server" },
  smart_exporter: { label: "SMART", icon: "disk" },
  cloudflare: { label: "Cloudflare", icon: "cloud" },
  cloudflared: { label: "cloudflared", icon: "cloud" },
  emby: { label: "Emby", icon: "play" },
};

function deviceType(typeId: string | undefined): { label: string; icon: Icon } {
  if (typeId && Object.hasOwn(DEVICE_TYPES, typeId)) {
    return DEVICE_TYPES[typeId] as { label: string; icon: Icon };
  }
  return { label: typeId ?? "Device", icon: "server" };
}

// Stroke icons, 24 x 24.
const ICON_PATHS: Record<Icon, string[]> = {
  shield: ["M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"],
  router: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z", "M8 12h8M12 8v8"],
  switch: [
    "M4 7h16a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z",
    "M6 12h.01M10 12h.01M14 12h.01M18 12h.01",
  ],
  pulse: ["M3 12h4l3-7 4 14 3-7h4"],
  globe: [
    "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z",
    "M3 12h18M12 3c2.5 2.7 2.5 15.3 0 18M12 3c-2.5 2.7-2.5 15.3 0 18",
  ],
  server: ["M4 4h16v6H4zM4 14h16v6H4z", "M8 7h.01M8 17h.01"],
  disk: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z", "M12 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4z"],
  cloud: ["M7 18a4 4 0 0 1-.5-8 6 6 0 0 1 11.5 1.5A3.5 3.5 0 0 1 17.5 18z"],
  play: ["M5 4h14v16H5z", "M10 9l5 3-5 3z"],
};

function TypeIcon({ icon }: { icon: Icon }) {
  return (
    <span className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-lg bg-chip text-muted">
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {ICON_PATHS[icon].map((d) => (
          <path key={d} d={d} />
        ))}
      </svg>
    </span>
  );
}

/** Now in Unix seconds, updated every 30 s (for "DOWN 12m"). */
function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

/** "12m", "3h", "2d": how long, in its largest unit. */
function shortAge(seconds: number): string {
  if (seconds >= 86400) return `${Math.floor(seconds / 86400)}d`;
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.max(1, Math.floor(seconds / 60))}m`;
}

const toSeconds = (iso: string | null | undefined) => {
  const t = iso ? Date.parse(iso) / 1000 : NaN;
  return Number.isFinite(t) ? t : undefined;
};

export interface CardHeaderProps {
  instance: Instance | undefined;
  freshness: Freshness;
  /** The device's uptime, when the card knows it. */
  uptimeSeconds?: number;
  /** A card-specific problem shown as the state ("100% LOSS"), in the critical colour. */
  problem?: string;
  /** A small card's one line: the name and the state, no icon or subtitle. */
  small?: boolean;
}

export function CardHeader({
  instance,
  freshness,
  uptimeSeconds,
  problem,
  small = false,
}: CardHeaderProps) {
  const state = shownState(instance, useScrapeStates([instance]));
  const type = deviceType(instance?.type_id);
  const now = useMinuteClock();
  // When the shown values stopped: the stale values' time, else the last successful poll.
  const lastSeen =
    freshness.kind === "stale" ? freshness.asOf : toSeconds(instance?.status.last_success_at);

  let text: string;
  let bad = true;
  if (problem) text = problem;
  else if (state === "down") text = lastSeen ? `DOWN ${shortAge(now - lastSeen)}` : "DOWN";
  else if (freshness.kind === "stale" || (state === "up" && freshness.kind === "missing"))
    text = "NO DATA";
  else {
    bad = false;
    text = state === "up" ? "UP" : state === "pending" ? "PENDING" : "UNKNOWN";
  }

  const sub =
    (state === "down" || freshness.kind === "stale") && lastSeen
      ? `no data since ${formatAsOf(lastSeen)}`
      : !bad && uptimeSeconds !== undefined && Number.isFinite(uptimeSeconds)
        ? `up ${formatDuration(uptimeSeconds)}`
        : undefined;
  const error = instance?.status.error ?? undefined;
  const stateText = (
    <span
      data-state={state}
      title={error}
      className={`shrink-0 text-xs tracking-wide whitespace-nowrap ${bad ? "font-semibold text-crit-text" : "text-muted"}`}
    >
      <span className="sr-only">Status: </span>
      {text}
    </span>
  );

  if (small) {
    return (
      <div className="flex min-w-0 items-baseline gap-2 leading-[18px]">
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold">
          {instance?.name ?? "Unknown device"}
        </span>
        {stateText}
      </div>
    );
  }
  return (
    <div className="flex min-w-0 items-center gap-3">
      <TypeIcon icon={type.icon} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[15px] font-semibold">
          {instance?.name ?? "Unknown device"}
        </div>
        <div className="truncate text-xs text-muted">
          {type.label}
          {sub && ` · ${sub}`}
        </div>
      </div>
      {stateText}
    </div>
  );
}
