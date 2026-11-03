import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";
import { Link, useParams } from "react-router";
import { ApiError } from "../api";
import type { CardPlacement, Dashboard, Instance } from "../api";
import { useApiClient } from "../app/session";
import { DashboardGrid } from "../cards/DashboardGrid";
import { MainDashboardContext } from "../cards/mainDashboard";
import {
  addCard,
  cardKey,
  DESKTOP_MIN_WIDTH,
  LAYOUT_VERSION,
  prepareLayout,
} from "../cards/layout";
import { cardFullSize, widget, WIDGETS } from "../cards/widgets";
import type { AvailableCard } from "../cards/layout";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError, buttonPrimary, buttonSecondary } from "../components/styles";
import { dashboardKey, DEFAULT_DASHBOARD_ID, useDashboards } from "./dashboards";
import { DashboardTabs } from "./DashboardTabs";
import { errorText, useInstances } from "./devices/queries";

function subscribeResize(onChange: () => void) {
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}

/** Editing is offered on desktop widths only (the phone layout is one fixed column). */
function useIsDesktop() {
  return useSyncExternalStore(subscribeResize, () => window.innerWidth >= DESKTOP_MIN_WIDTH);
}

interface Addable {
  key: string;
  card: { instance_id: string | null; card_type: string };
  title: string;
  size: [number, number];
}

/** The cards that can still be added, grouped: dashboard-wide first, then per device. */
function addableGroups(
  shown: CardPlacement[],
  instances: Instance[],
  available: AvailableCard[],
): { name: string; cards: Addable[] }[] {
  const placed = new Set(shown.map(cardKey));
  const sized = (card: Addable["card"], defaultSize: [number, number]): [number, number] => {
    // A card is added showing everything; the admin can make it smaller.
    const [fullW, fullH] = cardFullSize(card.card_type, card.instance_id);
    return [Math.max(defaultSize[0], fullW), Math.max(defaultSize[1], fullH)];
  };
  const groups = [
    {
      name: "Dashboard",
      cards: WIDGETS.map((w) => {
        const card = { instance_id: null, card_type: w.card_type };
        return { key: cardKey(card), card, title: w.title, size: sized(card, w.default_size) };
      }),
    },
    ...instances.map((instance) => ({
      name: instance.name,
      cards: available
        .filter((a) => a.instanceId === instance.id)
        .map(({ spec }) => {
          const card = { instance_id: instance.id, card_type: spec.card_type };
          return {
            key: cardKey(card),
            card,
            title: spec.title,
            size: sized(card, spec.default_size),
          };
        }),
    })),
  ];
  return groups
    .map((g) => ({ ...g, cards: g.cards.filter((c) => !placed.has(c.key)) }))
    .filter((g) => g.cards.length > 0);
}

function AddCardPanel({
  groups,
  onAdd,
}: {
  groups: ReturnType<typeof addableGroups>;
  onAdd: (a: Addable) => void;
}) {
  return (
    <section
      aria-label="Add a card"
      className="mb-4 flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
    >
      <h2 className="text-sm font-semibold">Add a card</h2>
      {groups.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Every card is on the dashboard. Add a device to get more.
        </p>
      ) : (
        groups.map((g) => (
          <div key={g.name} className="flex flex-col gap-1.5">
            <span className="text-xs tracking-wide text-slate-500 uppercase dark:text-slate-400">
              {g.name}
            </span>
            <div className="flex flex-wrap gap-2">
              {g.cards.map((a) => (
                <button
                  key={a.key}
                  type="button"
                  className={buttonSecondary}
                  onClick={() => onAdd(a)}
                >
                  + {a.title}
                </button>
              ))}
            </div>
          </div>
        ))
      )}
    </section>
  );
}

function CardSettingsPanel({
  card,
  instances,
  onChange,
  onClose,
}: {
  card: CardPlacement | undefined;
  instances: Instance[];
  onChange: (settings: CardPlacement["settings"]) => void;
  onClose: () => void;
}) {
  const spec = card && card.instance_id === null ? widget(card.card_type) : undefined;
  const Editor = spec?.settingsEditor;
  if (!card || !spec || !Editor) return null;
  return (
    <section
      aria-label={`${spec.title} settings`}
      className="mb-4 flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{spec.title} settings</h2>
        <button type="button" className={buttonSecondary} onClick={onClose}>
          Close
        </button>
      </div>
      <Editor instances={instances} settings={card.settings} onChange={onChange} />
    </section>
  );
}

/**
 * `/` shows the main dashboard (the first one), `/d/<id>` any dashboard. Each dashboard gets
 * its own view (keyed by id), so switching tabs never carries an edit over.
 */
export function DashboardPage() {
  const { id } = useParams();
  const dashboards = useDashboards();
  let dashboardId: number | null | undefined;
  if (id !== undefined) {
    dashboardId = /^[1-9]\d{0,9}$/.test(id) ? Number(id) : null;
  } else if (dashboards.data) {
    dashboardId = dashboards.data[0]?.id ?? DEFAULT_DASHBOARD_ID;
  } else if (dashboards.isError) {
    dashboardId = DEFAULT_DASHBOARD_ID;
  }
  return (
    <DashboardView key={String(dashboardId)} dashboardId={dashboardId} atMain={id === undefined} />
  );
}

/** One dashboard: `undefined` while the main one is being looked up, null for a bad address. */
function DashboardView({
  dashboardId,
  atMain,
}: {
  dashboardId: number | null | undefined;
  atMain: boolean;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const isDesktop = useIsDesktop();
  const instances = useInstances();
  const queryKey = dashboardKey(dashboardId ?? 0);
  const dashboard = useQuery({
    queryKey,
    queryFn: () => api.dashboard(dashboardId ?? 0),
    enabled: typeof dashboardId === "number",
  });
  const notFound =
    dashboardId === null || (dashboard.error instanceof ApiError && dashboard.error.status === 404);
  // The backend pushes live rates for the main dashboard's throughput card only.
  const list = useDashboards().data;
  const main = list ? (list[0]?.id ?? DEFAULT_DASHBOARD_ID) : undefined;
  const isMain =
    typeof dashboardId === "number" && (main === undefined ? atMain : main === dashboardId);
  // While editing: the layout being edited, saved only on Done.
  const [draft, setDraft] = useState<CardPlacement[] | null>(null);
  const [adding, setAdding] = useState(false);
  // The card whose settings are open, by cardKey (edit mode only).
  const [configuring, setConfiguring] = useState<string | null>(null);

  const instanceList = instances.data ?? [];
  const cardQueries = useQueries({
    queries: instanceList.map((i) => ({
      queryKey: ["instances", i.id, "cards"],
      queryFn: () => api.instanceCards(i.id),
      // Card specs only change with a backend upgrade.
      staleTime: Infinity,
    })),
  });

  const save = useMutation({
    mutationFn: (cards: CardPlacement[]) =>
      api.saveDashboard(dashboardId ?? 0, { version: LAYOUT_VERSION, cards }),
    // Show the new layout at once so the grid doesn't snap back while the request runs.
    onMutate: (cards) => {
      queryClient.setQueryData<Dashboard>(queryKey, (old) =>
        old ? { ...old, layout: { version: LAYOUT_VERSION, cards } } : old,
      );
    },
    onError: () => queryClient.invalidateQueries({ queryKey }),
  });

  const available: AvailableCard[] = [];
  const loaded = new Set<string>();
  const titles = new Map<string, string>();
  instanceList.forEach((instance, i) => {
    const specs = cardQueries[i]?.data;
    if (!specs) return;
    loaded.add(instance.id);
    for (const spec of specs) {
      available.push({ instanceId: instance.id, spec });
      titles.set(cardKey({ instance_id: instance.id, card_type: spec.card_type }), spec.title);
    }
  });
  const known = new Set(instanceList.map((i) => i.id));
  const saved =
    dashboard.data && instances.data
      ? prepareLayout(dashboard.data.layout, WIDGETS, available, known, loaded)
      : null;

  const editing = draft !== null && isDesktop;
  const shown = editing ? draft : saved;

  const startEditing = (openAdd: boolean) => {
    setDraft(saved ?? []);
    setAdding(openAdd);
  };
  const stopEditing = () => {
    setDraft(null);
    setAdding(false);
    setConfiguring(null);
  };
  const done = () => {
    if (draft) save.mutate(draft);
    stopEditing();
  };

  let actions = null;
  if (isDesktop && saved) {
    actions = editing ? (
      <div className="flex gap-2">
        <button type="button" className={buttonSecondary} onClick={() => setAdding(!adding)}>
          {adding ? "Hide card list" : "Add card"}
        </button>
        <button type="button" className={buttonSecondary} onClick={stopEditing}>
          Cancel
        </button>
        <button type="button" className={buttonPrimary} onClick={done}>
          Done
        </button>
      </div>
    ) : (
      <button type="button" className={buttonSecondary} onClick={() => startEditing(false)}>
        Edit dashboard
      </button>
    );
  }

  let body;
  if (notFound) {
    body = (
      <EmptyState>
        <p>Dashboard not found.</p>
        <Link to="/" className="mt-2 inline-block text-in underline">
          Open the main dashboard
        </Link>
      </EmptyState>
    );
  } else if (instances.isPending || dashboard.isPending) {
    body = (
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading…
      </p>
    );
  } else if (instances.isError || dashboard.isError) {
    body = (
      <p role="alert" className={alertError}>
        {errorText(instances.error ?? dashboard.error)}
      </p>
    );
  } else if (shown && shown.length === 0 && !editing) {
    body = (
      <EmptyState>
        <p>The dashboard is empty.</p>
        {isDesktop && (
          <button
            type="button"
            className={`mt-3 ${buttonPrimary}`}
            onClick={() => startEditing(true)}
          >
            Add your first card
          </button>
        )}
      </EmptyState>
    );
  } else if (shown) {
    body = (
      <DashboardGrid
        cards={shown}
        instances={new Map(instanceList.map((i) => [i.id, i]))}
        titles={titles}
        editing={editing}
        compact={!isDesktop}
        onChange={setDraft}
        onRemove={(card) => setDraft(shown.filter((c) => cardKey(c) !== cardKey(card)))}
        onConfigure={(card) => {
          setAdding(false);
          setConfiguring(cardKey(card));
        }}
      />
    );
  }

  return (
    <MainDashboardContext.Provider value={isMain}>
      <PageHeader title="Dashboard">{actions}</PageHeader>
      <DashboardTabs
        current={dashboardId ?? null}
        currentName={dashboard.data?.name}
        editing={editing}
        atMain={atMain}
      />
      {save.isError && (
        <p role="alert" className={`mb-4 ${alertError}`}>
          {errorText(save.error)}
        </p>
      )}
      {editing && draft && configuring && (
        <CardSettingsPanel
          card={draft.find((c) => cardKey(c) === configuring)}
          instances={instanceList}
          onChange={(settings) =>
            setDraft(draft.map((c) => (cardKey(c) === configuring ? { ...c, settings } : c)))
          }
          onClose={() => setConfiguring(null)}
        />
      )}
      {editing && adding && draft && (
        <AddCardPanel
          groups={addableGroups(draft, instanceList, available)}
          onAdd={(a) => setDraft(addCard(draft, a.card, a.size))}
        />
      )}
      {body}
    </MainDashboardContext.Provider>
  );
}
