import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import type { NetworkMap, NetworkMapSummary } from "../api";
import { useApiClient, useUser } from "../app/session";
import { EmptyState } from "../components/PageHeader";
import { alertError, buttonPrimary, buttonSecondary, input } from "../components/styles";
import { errorText } from "./devices/queries";
import { MapEditor } from "./maps/editor/MapEditor";
import { NewMapForm } from "./maps/editor/NewMapForm";
import { MapCanvas } from "./maps/MapCanvas";
import { useMapLive } from "./maps/mapLive";
import { MapPhoneList } from "./maps/MapPhoneList";
import { useMediaQuery, WIDE_QUERY } from "./maps/useMediaQuery";

const MAPS_KEY = ["maps", "list"] as const;
const mapKey = (id: number) => ["maps", id] as const;
/** Maps change only when an admin saves; the tabs and drawing are refetched now and then. */
const MAP_REFRESH_MS = 60_000;

const tabClass = (active: boolean) =>
  `-mb-px border-b-2 px-3 py-1.5 whitespace-nowrap focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none focus-visible:ring-inset ${
    active ? "border-in font-medium text-ink" : "border-transparent text-muted hover:text-ink"
  }`;

function MapTabs({
  list,
  current,
  isAdmin,
  editing,
  onCreated,
}: {
  list: NetworkMapSummary[];
  current: number | null;
  isAdmin: boolean;
  /** The current map is being edited: the tabs can't change under the editor. */
  editing: boolean;
  onCreated: (map: NetworkMap, warning?: string) => void;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [form, setForm] = useState<"new" | "rename" | null>(null);
  const index = list.findIndex((m) => m.id === current);
  const name = list[index]?.name ?? "";

  const refreshList = () => queryClient.invalidateQueries({ queryKey: MAPS_KEY });
  const update = useMutation({
    mutationFn: (body: { name?: string; position?: number }) => api.updateMap(current ?? 0, body),
    onSuccess: async () => {
      setForm(null);
      if (current !== null) await queryClient.invalidateQueries({ queryKey: mapKey(current) });
      await refreshList();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteMap(current ?? 0),
    onSuccess: async () => {
      if (current !== null) queryClient.removeQueries({ queryKey: mapKey(current) });
      await refreshList();
      void navigate("/map");
    },
  });
  const onDelete = () => {
    if (window.confirm(`Delete the map "${name}"? Its drawing is lost.`)) remove.mutate();
  };
  const manage = isAdmin && !editing && index >= 0;
  const failed = update.error ?? remove.error;

  return (
    <div className="mb-4 md:mb-0 md:shrink-0 md:bg-panel md:px-4 md:pt-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 border-b border-line md:border-b-0">
        <nav aria-label="Maps" className="hidden flex-wrap md:flex">
          {list.map((m, i) => (
            <Link
              key={m.id}
              to={i === 0 ? "/map" : `/map/${m.id}`}
              aria-current={m.id === current ? "page" : undefined}
              className={tabClass(m.id === current)}
            >
              {m.name}
            </Link>
          ))}
        </nav>
        {list.length > 0 && (
          <label className="flex items-center gap-2 pb-2 text-sm md:hidden">
            <span className="text-muted">Map</span>
            <select
              className={`${input} w-auto`}
              value={current === null ? "" : String(current)}
              onChange={(e) => void navigate(`/map/${e.target.value}`)}
            >
              {current === null && <option value="">Choose…</option>}
              {list.map((m) => (
                <option key={m.id} value={String(m.id)}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {isAdmin && !editing && (
          <button
            type="button"
            className={`${tabClass(false)} hidden md:block`}
            onClick={() => setForm(form === "new" ? null : "new")}
          >
            + New map
          </button>
        )}
        {manage && (
          <div className="ml-auto hidden gap-2 pb-2 md:flex">
            <button type="button" className={buttonSecondary} onClick={() => setForm("rename")}>
              Rename
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={index === 0 || update.isPending}
              onClick={() => update.mutate({ position: index - 1 })}
            >
              Move left
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={index === list.length - 1 || update.isPending}
              onClick={() => update.mutate({ position: index + 1 })}
            >
              Move right
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={remove.isPending}
              onClick={onDelete}
            >
              Delete
            </button>
          </div>
        )}
      </div>
      {form === "new" && !editing && (
        <div className="mt-3 md:pb-3">
          <NewMapForm maps={list} onCreated={onCreated} onCancel={() => setForm(null)} />
        </div>
      )}
      {form === "rename" && manage && (
        <RenameForm
          initial={name}
          busy={update.isPending}
          onSubmit={(n) => update.mutate({ name: n })}
          onCancel={() => setForm(null)}
        />
      )}
      {failed && (
        <p role="alert" className={`mt-2 md:mb-3 ${alertError}`}>
          {errorText(failed)}
        </p>
      )}
    </div>
  );
}

function RenameForm({
  initial,
  busy,
  onSubmit,
  onCancel,
}: {
  initial: string;
  busy: boolean;
  onSubmit: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) onSubmit(name.trim());
  };
  return (
    <form
      aria-label="Rename map"
      onSubmit={submit}
      className="flex flex-wrap items-center gap-2 py-2 text-sm"
    >
      <label className="flex items-center gap-2">
        <span>Name</span>
        <input
          className={`${input} w-56`}
          value={name}
          maxLength={64}
          required
          autoFocus
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <button type="submit" className={buttonPrimary} disabled={busy || !name.trim()}>
        Save
      </button>
      <button type="button" className={buttonSecondary} onClick={onCancel}>
        Cancel
      </button>
    </form>
  );
}

/** The working copy starts from the map as it was when editing began; refreshes don't touch it. */
function EditSession({
  map,
  onDone,
  onReload,
}: {
  map: NetworkMap;
  onDone: (saved: NetworkMap | null) => void;
  onReload: () => void;
}) {
  const [frozen] = useState(map);
  return (
    <MapEditor map={frozen} onSaved={onDone} onClose={() => onDone(null)} onReload={onReload} />
  );
}

function MapView({
  id,
  isAdmin,
  editing,
  onEdit,
  onStopEdit,
}: {
  id: number;
  isAdmin: boolean;
  editing: boolean;
  onEdit: () => void;
  onStopEdit: () => void;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const map = useQuery({
    queryKey: mapKey(id),
    queryFn: () => api.map(id),
    refetchInterval: editing ? false : MAP_REFRESH_MS,
  });
  // Draw on a wide screen, list on a phone: only one of the two is rendered (and queried).
  const wide = useMediaQuery(WIDE_QUERY);
  const live = useMapLive(map.data?.document, wide);
  if (map.isPending) return <p className="text-sm text-muted md:p-6">Loading the map…</p>;
  if (map.isError) {
    return (
      <div className="md:p-6">
        <p role="alert" className={alertError}>
          {errorText(map.error)}
        </p>
      </div>
    );
  }
  const canEdit = isAdmin && wide;
  if (editing && canEdit) {
    return (
      <EditSession
        map={map.data}
        onDone={(saved) => {
          if (saved) {
            queryClient.setQueryData(mapKey(id), saved);
            void queryClient.invalidateQueries({ queryKey: MAPS_KEY });
          }
          onStopEdit();
        }}
        onReload={() => {
          void queryClient.invalidateQueries({ queryKey: mapKey(id) }).then(onStopEdit);
        }}
      />
    );
  }
  const doc = map.data.document;
  const editButton = canEdit && (
    <button type="button" className={buttonSecondary} onClick={onEdit}>
      Edit map
    </button>
  );
  if (doc.elements.length === 0) {
    return (
      <div className="md:p-6">
        <EmptyState>
          <p className="mb-3">This map is empty.</p>
          {editButton}
        </EmptyState>
      </div>
    );
  }
  return wide ? (
    <MapCanvas doc={doc} live={live} actions={editButton || undefined} />
  ) : (
    <MapPhoneList doc={doc} live={live} />
  );
}

/** `/map` shows the first map, `/map/<id>` any map. */
export function MapPage() {
  const { id } = useParams();
  const api = useApiClient();
  const isAdmin = useUser().role === "admin";
  const maps = useQuery({
    queryKey: MAPS_KEY,
    queryFn: () => api.maps(),
    refetchInterval: MAP_REFRESH_MS,
  });
  const navigate = useNavigate();
  // The map being edited, if any (it stays set while the address changes to the new map).
  const [editingId, setEditingId] = useState<number | null>(null);
  const [createWarning, setCreateWarning] = useState<string | null>(null);
  const list = maps.data ?? [];
  const wanted =
    id === undefined ? (list[0]?.id ?? null) : /^[1-9]\d{0,9}$/.test(id) ? Number(id) : null;
  const known = wanted !== null && list.some((m) => m.id === wanted);

  const queryClient = useQueryClient();
  // Leaving the map that is being edited (a tab, after confirming) ends the editing, so
  // coming back shows the map, not a fresh editor.
  const lastWanted = useRef(wanted);
  useEffect(() => {
    if (lastWanted.current !== wanted && lastWanted.current === editingId) {
      setEditingId(null);
      setCreateWarning(null);
    }
    lastWanted.current = wanted;
  }, [wanted, editingId]);

  const onCreated = async (created: NetworkMap, warning?: string) => {
    setCreateWarning(warning ?? null);
    queryClient.setQueryData(mapKey(created.id), created);
    await queryClient.invalidateQueries({ queryKey: MAPS_KEY });
    setEditingId(created.id);
    void navigate(`/map/${created.id}`);
  };

  let body;
  if (maps.isPending) body = <p className="text-sm text-muted md:p-6">Loading the maps…</p>;
  else if (maps.isError) {
    body = (
      <div className="md:p-6">
        <p role="alert" className={alertError}>
          {errorText(maps.error)}
        </p>
      </div>
    );
  } else if (list.length === 0) {
    body = (
      <div className="md:p-6">
        <NoMaps isAdmin={isAdmin} onCreated={onCreated} />
      </div>
    );
  } else if (!known || wanted === null) {
    body = <p className="text-sm text-muted md:p-6">There is no such map.</p>;
  } else {
    body = (
      <MapView
        key={wanted}
        id={wanted}
        isAdmin={isAdmin}
        editing={editingId === wanted}
        onEdit={() => setEditingId(wanted)}
        onStopEdit={() => {
          setEditingId(null);
          setCreateWarning(null);
        }}
      />
    );
  }

  // On desktop the page fills the whole area next to the sidebar; the drawing takes what's left.
  return (
    <div className="md:flex md:min-h-0 md:flex-1 md:flex-col">
      {list.length > 0 && (
        <MapTabs
          list={list}
          current={known ? wanted : null}
          isAdmin={isAdmin}
          editing={editingId !== null && editingId === wanted}
          onCreated={onCreated}
        />
      )}
      {createWarning && editingId !== null && (
        <p role="alert" className={`mb-3 md:mx-4 md:mt-3 ${alertError}`}>
          The map was created, but its devices could not be added: {createWarning}. Add them from
          the list, or press Cancel and try again from a blank map.
        </p>
      )}
      {body}
    </div>
  );
}

function NoMaps({
  isAdmin,
  onCreated,
}: {
  isAdmin: boolean;
  onCreated: (map: NetworkMap, warning?: string) => void;
}) {
  const [creating, setCreating] = useState(false);
  if (creating) {
    return <NewMapForm maps={[]} onCreated={onCreated} onCancel={() => setCreating(false)} />;
  }
  return (
    <EmptyState>
      <p className="mb-3 text-base font-medium text-ink">No maps yet</p>
      {isAdmin ? (
        <>
          <p className="mb-3">Draw your network: devices, how they connect, notes and tables.</p>
          <button type="button" className={buttonPrimary} onClick={() => setCreating(true)}>
            New map
          </button>
        </>
      ) : (
        <p>An admin can draw the first map.</p>
      )}
    </EmptyState>
  );
}
