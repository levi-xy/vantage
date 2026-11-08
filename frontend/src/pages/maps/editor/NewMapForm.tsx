// The new-map flow: a name, then blank, "start with my devices" (one linked box per device,
// in rows by site) or a copy of another map. The new map opens ready to edit.

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import type { NetworkMap, NetworkMapSummary } from "../../../api";
import { useApiClient } from "../../../app/session";
import { alertError, buttonPrimary, buttonSecondary, input } from "../../../components/styles";
import { errorText } from "../../devices/queries";
import { devicesDocument } from "./model";

type Start = "blank" | "devices" | "copy";

const OPTIONS: { id: Start; title: string; hint: string }[] = [
  { id: "blank", title: "Blank canvas", hint: "Start empty and place everything yourself." },
  {
    id: "devices",
    title: "Start with my devices",
    hint: "One box per device, in rows by site. No cables yet: you arrange and connect them.",
  },
  { id: "copy", title: "Copy a map", hint: "" },
];

export function NewMapForm({
  maps,
  onCreated,
  onCancel,
}: {
  maps: readonly NetworkMapSummary[];
  onCreated: (map: NetworkMap) => void;
  onCancel: () => void;
}) {
  const api = useApiClient();
  const [name, setName] = useState("");
  const [start, setStart] = useState<Start>("blank");
  const [copyFrom, setCopyFrom] = useState<number | null>(maps[0]?.id ?? null);

  const create = useMutation({
    mutationFn: async (): Promise<NetworkMap> => {
      const trimmed = name.trim();
      if (start === "copy" && copyFrom !== null) {
        return api.createMap({ name: trimmed, copy_from: copyFrom });
      }
      const created = await api.createMap({ name: trimmed });
      if (start !== "devices") return created;
      const [instances, types] = await Promise.all([api.instances(), api.integrations()]);
      const document = devicesDocument(
        instances,
        (typeId) => types.find((t) => t.type_id === typeId)?.category,
      );
      return api.saveMap(created.id, { document, revision: created.revision });
    },
    onSuccess: onCreated,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) create.mutate();
  };

  return (
    <form
      aria-label="New map"
      onSubmit={submit}
      className="mb-4 flex max-w-xl flex-col gap-3 rounded-lg border border-line bg-panel p-4"
    >
      <h2 className="text-base font-semibold">New network map</h2>
      <p className="text-sm text-muted">
        Draw your network the way you think of it. You can add, move or delete anything later.
      </p>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">Name</span>
        <input
          className={input}
          value={name}
          maxLength={64}
          required
          autoFocus
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">How to start</legend>
        {OPTIONS.map((o) => {
          const disabled = o.id === "copy" && maps.length === 0;
          return (
            <label
              key={o.id}
              className={`flex items-start gap-2 rounded-md border border-line p-2.5 text-sm ${disabled ? "opacity-60" : ""}`}
            >
              <input
                type="radio"
                name="start"
                className="mt-1"
                checked={start === o.id}
                disabled={disabled}
                onChange={() => setStart(o.id)}
              />
              <span className="min-w-0">
                <b className="block">{o.title}</b>
                {o.id === "copy" ? (
                  <select
                    className={`${input} mt-1`}
                    aria-label="Map to copy"
                    value={copyFrom === null ? "" : String(copyFrom)}
                    disabled={start !== "copy"}
                    onChange={(e) => setCopyFrom(Number(e.target.value))}
                  >
                    {maps.map((m) => (
                      <option key={m.id} value={String(m.id)}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="text-muted">{o.hint}</span>
                )}
              </span>
            </label>
          );
        })}
      </fieldset>
      {create.error && (
        <p role="alert" className={alertError}>
          {errorText(create.error)}
        </p>
      )}
      <div className="flex items-center gap-2">
        <button type="submit" className={buttonPrimary} disabled={create.isPending || !name.trim()}>
          Create and start editing
        </button>
        <button type="button" className={buttonSecondary} onClick={onCancel}>
          Cancel
        </button>
        <span className="ml-auto text-xs text-muted">
          Maps are shared with everyone who can log in.
        </span>
      </div>
    </form>
  );
}
