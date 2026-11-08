// One port's VLANs in the inspector: the untagged one, and the tagged ones, from the map's
// VLAN list.

import type { MapDocument, MapPort } from "../../../api";
import { input } from "../../../components/styles";
import { Check, labelText } from "./fields";
import { MAX_TAGGED } from "./mapOps";

export function PortVlans({
  name,
  vlans,
  choices,
  onChange,
}: {
  name: string;
  vlans: MapPort["vlans"];
  choices: MapDocument["vlans"];
  onChange: (v: MapPort["vlans"]) => void;
}) {
  if (choices.length === 0) {
    return <p className="text-xs text-muted">Add VLANs with the VLANs button to set them here.</p>;
  }
  return (
    <div className="flex flex-col gap-1.5">
      <select
        className={input}
        aria-label={`${name} untagged VLAN`}
        value={vlans.untagged ?? ""}
        onChange={(e) => {
          const untagged = e.target.value || null;
          onChange({ untagged, tagged: vlans.tagged.filter((t) => t !== untagged) });
        }}
      >
        <option value="">No untagged VLAN</option>
        {choices.map((v) => (
          <option key={v.id} value={v.id}>
            {v.id} · {v.name}
          </option>
        ))}
      </select>
      <span className={labelText}>Tagged</span>
      {choices.map((v) => {
        const on = vlans.tagged.includes(v.id);
        return (
          <Check
            key={v.id}
            label={`${name} tagged VLAN ${v.id}`}
            checked={on}
            disabled={v.id === vlans.untagged || (!on && vlans.tagged.length >= MAX_TAGGED)}
            onChange={(yes) =>
              onChange({
                untagged: vlans.untagged,
                tagged: yes ? [...vlans.tagged, v.id] : vlans.tagged.filter((t) => t !== v.id),
              })
            }
          />
        );
      })}
    </div>
  );
}
