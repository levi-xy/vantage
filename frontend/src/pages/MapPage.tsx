import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";
import type { NetworkMapSummary } from "../api";
import { useApiClient, useUser } from "../app/session";
import { EmptyState } from "../components/PageHeader";
import { alertError, buttonPrimary, input } from "../components/styles";
import { errorText } from "./devices/queries";
import { MapCanvas } from "./maps/MapCanvas";
import { useMapLive } from "./maps/mapLive";
import { MapPhoneList } from "./maps/MapPhoneList";

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
}: {
  list: NetworkMapSummary[];
  current: number | null;
  isAdmin: boolean;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const create = useMutation({
    // The editor (M9.3) renames it; for now a blank map is added and shown.
    mutationFn: () => api.createMap({ name: `Map ${list.length + 1}` }),
    onSuccess: async (created) => {
      queryClient.setQueryData(mapKey(created.id), created);
      await queryClient.invalidateQueries({ queryKey: MAPS_KEY });
      void navigate(`/map/${created.id}`);
    },
  });
  return (
    <div className="mb-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 border-b border-line">
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
        {isAdmin && (
          <button
            type="button"
            className={`${tabClass(false)} hidden md:block`}
            disabled={create.isPending}
            onClick={() => create.mutate()}
          >
            + New map
          </button>
        )}
      </div>
      {create.error && (
        <p role="alert" className={`mt-2 ${alertError}`}>
          {errorText(create.error)}
        </p>
      )}
    </div>
  );
}

function MapView({ id }: { id: number }) {
  const api = useApiClient();
  const map = useQuery({
    queryKey: mapKey(id),
    queryFn: () => api.map(id),
    refetchInterval: MAP_REFRESH_MS,
  });
  const live = useMapLive(map.data?.document);
  if (map.isPending) return <p className="text-sm text-muted">Loading the map…</p>;
  if (map.isError) {
    return (
      <p role="alert" className={alertError}>
        {errorText(map.error)}
      </p>
    );
  }
  const doc = map.data.document;
  if (doc.elements.length === 0) {
    return <EmptyState>This map is empty. Drawing comes with the map editor.</EmptyState>;
  }
  return (
    <>
      <div className="hidden md:block">
        <MapCanvas doc={doc} live={live} />
      </div>
      <div className="md:hidden">
        <MapPhoneList doc={doc} live={live} />
      </div>
    </>
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
  const list = maps.data ?? [];
  const wanted =
    id === undefined ? (list[0]?.id ?? null) : /^[1-9]\d{0,9}$/.test(id) ? Number(id) : null;
  const known = wanted !== null && list.some((m) => m.id === wanted);

  let body;
  if (maps.isPending) body = <p className="text-sm text-muted">Loading the maps…</p>;
  else if (maps.isError) {
    body = (
      <p role="alert" className={alertError}>
        {errorText(maps.error)}
      </p>
    );
  } else if (list.length === 0) {
    body = <NoMaps isAdmin={isAdmin} />;
  } else if (!known || wanted === null) {
    body = <p className="text-sm text-muted">There is no such map.</p>;
  } else {
    body = <MapView key={wanted} id={wanted} />;
  }

  return (
    <>
      {list.length > 0 && <MapTabs list={list} current={known ? wanted : null} isAdmin={isAdmin} />}
      {body}
    </>
  );
}

function NoMaps({ isAdmin }: { isAdmin: boolean }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const create = useMutation({
    mutationFn: () => api.createMap({ name: "Map 1" }),
    onSuccess: async (created) => {
      queryClient.setQueryData(mapKey(created.id), created);
      await queryClient.invalidateQueries({ queryKey: MAPS_KEY });
      void navigate(`/map/${created.id}`);
    },
  });
  return (
    <EmptyState>
      <p className="mb-3 text-base font-medium text-ink">No maps yet</p>
      {isAdmin ? (
        <>
          <p className="mb-3">Draw your network: devices, how they connect, notes and tables.</p>
          <button
            type="button"
            className={buttonPrimary}
            disabled={create.isPending}
            onClick={() => create.mutate()}
          >
            New map
          </button>
          {create.error && (
            <p role="alert" className={`mt-3 ${alertError}`}>
              {errorText(create.error)}
            </p>
          )}
        </>
      ) : (
        <p>An admin can draw the first map.</p>
      )}
    </EmptyState>
  );
}
