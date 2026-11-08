// The map's VLAN list: id, name and colour. Removing a VLAN that ports or cables use asks
// first, then clears it from them.

import type { MapDocument } from "../../../api";
import { DEVICE_COLORS } from "../../../app/deviceColors";
import { buttonSecondary, input } from "../../../components/styles";
import { CommitText } from "./fields";
import { MAX_VLANS, newVlan, removeVlan, renameVlan, vlanUsage } from "./mapOps";

export function VlanPanel({
  doc,
  edit,
  onClose,
}: {
  doc: MapDocument;
  edit: (next: MapDocument) => void;
  onClose: () => void;
}) {
  const remove = (id: string) => {
    const used = vlanUsage(doc, id);
    if (
      used > 0 &&
      !window.confirm(
        `VLAN ${id} is used in ${used} place${used === 1 ? "" : "s"} on this map. Remove it and clear it from them?`,
      )
    ) {
      return;
    }
    edit(removeVlan(doc, id));
  };
  return (
    <section aria-label="VLANs" className="rounded-lg border border-line bg-panel p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold">VLANs of this map</h2>
        <button type="button" className={buttonSecondary} onClick={onClose}>
          Close
        </button>
      </div>
      <p className="mb-2 text-xs text-muted">
        Ports and access cables pick from this list. The colour shows as bars under the ports.
      </p>
      <div className="flex flex-col gap-2">
        {doc.vlans.map((v) => (
          <div key={v.id} className="flex flex-col gap-1.5 rounded-md border border-line p-2">
            <div className="grid grid-cols-[5rem_minmax(0,1fr)] gap-1.5">
              <CommitText
                label={`VLAN ${v.id} id`}
                value={v.id}
                maxLength={16}
                accept={(x) => x.trim() !== "" && !doc.vlans.some((o) => o.id === x)}
                onCommit={(x) => edit(renameVlan(doc, v.id, x))}
              />
              <CommitText
                label={`VLAN ${v.id} name`}
                value={v.name}
                maxLength={32}
                onCommit={(name) =>
                  edit({
                    ...doc,
                    vlans: doc.vlans.map((o) => (o.id === v.id ? { ...o, name } : o)),
                  })
                }
              />
            </div>
            <div className="flex gap-1.5">
              <select
                className={input}
                aria-label={`VLAN ${v.id} colour`}
                value={v.color}
                onChange={(e) =>
                  edit({
                    ...doc,
                    vlans: doc.vlans.map((o) =>
                      o.id === v.id ? { ...o, color: e.target.value } : o,
                    ),
                  })
                }
              >
                {DEVICE_COLORS.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                aria-label={`Remove VLAN ${v.id}`}
                className={buttonSecondary}
                onClick={() => remove(v.id)}
              >
                ×
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className={buttonSecondary}
          disabled={doc.vlans.length >= MAX_VLANS}
          onClick={() => edit({ ...doc, vlans: [...doc.vlans, newVlan(doc)] })}
        >
          Add VLAN
        </button>
      </div>
    </section>
  );
}
