import { useState } from "react";
import { useUnits } from "../../app/units";
import { CardFrame } from "../metrics/CardFrame";
import type { CardProps } from "../registry";
import { describe, TILE, useSnmpPorts } from "../snmp-ports/ports";
import { isSfp, panelBlocks } from "./panel";

/** The switch's front panel: ports where they sit on the device, coloured by state. */
export function SwitchPanelCard({ instanceId, title, instance }: CardProps) {
  const { formatRate } = useUnits();
  const { ports, loading, error } = useSnmpPorts(instanceId);
  const [selected, setSelected] = useState<string | null>(null);
  const chosen = ports.find((p) => p.index === selected);

  return (
    <CardFrame
      title={title}
      instance={instance}
      loading={loading}
      error={error}
      empty={ports.length === 0}
    >
      <div
        role="group"
        aria-label="Front panel"
        className="flex w-fit max-w-full flex-wrap items-start gap-3 rounded-md bg-slate-100 p-2 dark:bg-slate-800"
      >
        {panelBlocks(ports).map((block, b) => (
          <div
            key={b}
            className="grid grid-rows-2 gap-1"
            style={{ gridTemplateColumns: `repeat(${Math.ceil(block.length / 2)}, 2rem)` }}
          >
            {block.map(({ port, column, row }) => {
              const sfp = isSfp(port);
              return (
                <button
                  key={port.index}
                  type="button"
                  style={{ gridColumn: column, gridRow: row }}
                  aria-pressed={selected === port.index}
                  aria-label={`${port.name}: ${port.state}`}
                  title={describe(port, formatRate)}
                  onClick={() => setSelected(selected === port.index ? null : port.index)}
                  className={`flex h-8 items-center justify-center border text-xs font-medium tabular-nums ${
                    sfp ? "rounded-none border-2" : "rounded-sm"
                  } ${TILE[port.state]} ${selected === port.index ? "ring-2 ring-blue-500" : ""}`}
                >
                  {port.index}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      {chosen && (
        <p className="text-xs text-slate-600 dark:text-slate-300" aria-live="polite">
          {describe(chosen, formatRate)}
        </p>
      )}
    </CardFrame>
  );
}
