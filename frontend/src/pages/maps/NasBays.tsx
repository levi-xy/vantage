// The drive bays on a NAS front: a tray per bay with slits, a handle and an LED (healthy /
// warm / failing; dark for a disk with no data; no slits for an empty bay), and under it the
// bay's name, the disk's size and its temperature when the SMART exporter reports the disk
// the bay is matched to. Plain text only.

import { formatBytes, formatCelsius } from "../../cards/metrics/format";
import { Clip } from "./boxParts";
import type { BoxModel } from "./boxParts";
import { bays } from "./smartDisks";
import type { Bay, BayState } from "./smartDisks";

const TRAY = {
  s: { w: 28, h: 84, slits: 7 },
  m: { w: 34, h: 104, slits: 9 },
  l: { w: 40, h: 124, slits: 11 },
} as const;

const LED: Record<BayState, string> = {
  healthy: "var(--vt-ok)",
  warm: "var(--vt-warn)",
  failing: "var(--vt-crit)",
  unknown: "var(--vt-port-off)",
  empty: "var(--vt-port-off)",
};

function Tray({ bay, size }: { bay: Bay; size: keyof typeof TRAY }) {
  const t = TRAY[size];
  const empty = bay.state === "empty";
  const led = LED[bay.state];
  return (
    <span className="flex flex-col items-center gap-[3px]" style={{ width: t.w }}>
      <span
        data-bay={bay.n}
        data-bay-state={bay.state}
        aria-label={`Bay ${bay.label}: ${bay.state}`}
        role="img"
        className="relative box-border block rounded-[3px] border border-line"
        style={{
          width: t.w,
          height: t.h,
          paddingTop: 8,
          background: empty
            ? "var(--vt-inset)"
            : "linear-gradient(180deg, var(--vt-chip), var(--vt-inset))",
        }}
      >
        {!empty &&
          Array.from({ length: t.slits }, (_, i) => (
            <span
              key={i}
              className="mx-1.5 mt-[3px] block rounded-[1px] bg-line"
              style={{ height: 2 }}
            />
          ))}
        <span
          className="absolute bottom-2 left-1/2 block h-1 w-4 -translate-x-1/2 rounded-sm border border-line bg-panel"
          style={{ height: 5 }}
        />
        <span
          data-led=""
          className="absolute top-1 right-1 block h-[5px] w-[5px] rounded-full"
          style={{
            background: led,
            boxShadow: bay.state === "unknown" || empty ? undefined : `0 0 4px ${led}`,
          }}
        />
      </span>
      <span className="block w-full text-center text-[9px] leading-[11px] text-muted">
        <Clip>{bay.label}</Clip>
        <Clip>
          {empty ? "empty" : bay.disk?.size !== undefined ? formatBytes(bay.disk.size) : ""}
        </Clip>
        <Clip>
          {bay.disk?.temperature !== undefined ? formatCelsius(bay.disk.temperature) : ""}
        </Clip>
      </span>
    </span>
  );
}

/** The bay column of a NAS faceplate; nothing without a `nas` setting. */
export function NasBays({ m }: { m: BoxModel }) {
  const { box, live, removed } = m;
  if (!box.nas || removed) return null;
  const size = box.panel?.size ?? "m";
  const list = bays(box.nas, live.smartDisks(box.instance_id));
  return (
    <div
      data-bays=""
      className="mr-3.5 ml-3.5 mt-6 flex h-fit max-w-[52%] shrink-0 flex-wrap gap-[5px] rounded-md border border-line bg-inset p-2"
    >
      {list.map((bay) => (
        <Tray key={bay.n} bay={bay} size={size} />
      ))}
    </div>
  );
}
