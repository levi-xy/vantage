import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";
import type { CardPlacement, Dashboard, Instance } from "../api";
import { useApiClient, useUser } from "../app/session";
import { DashboardGrid } from "../cards/DashboardGrid";
import {
  addCard,
  cardKey,
  DESKTOP_MIN_WIDTH,
  LAYOUT_VERSION,
  prepareLayout,
} from "../cards/layout";
import { cardMinSize, widget, WIDGETS } from "../cards/widgets";
import type { AvailableCard } from "../cards/layout";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError, buttonPrimary, buttonSecondary } from "../components/styles";
import { errorText, useInstances } from "./devices/queries";

/** The one dashboard there is (the backend creates it on first read). */
const DASHBOARD_ID = 1;
const DASHBOARD_KEY = ["dashboards", DASHBOARD_ID] as const;

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
    const [minW, minH] = cardMinSize(card.card_type, card.instance_id);
    return [Math.max(defaultSize[0], minW), Math.max(defaultSize[1], minH)];
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

export function DashboardPage() {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const isAdmin = useUser().role === "admin";
  const isDesktop = useIsDesktop();
  const instances = useInstances();
  const dashboard = useQuery({
    queryKey: DASHBOARD_KEY,
    queryFn: () => api.dashboard(DASHBOARD_ID),
  });
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
      api.saveDashboard(DASHBOARD_ID, { version: LAYOUT_VERSION, cards }),
    // Show the new layout at once so the grid doesn't snap back while the request runs.
    onMutate: (cards) => {
      queryClient.setQueryData<Dashboard>(DASHBOARD_KEY, (old) =>
        old ? { ...old, layout: { version: LAYOUT_VERSION, cards } } : old,
      );
    },
    onError: () => queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY }),
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

  const editing = draft !== null && isAdmin && isDesktop;
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
  if (isAdmin && isDesktop && saved) {
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
  if (instances.isPending || dashboard.isPending) {
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
        {isAdmin && isDesktop && (
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
    <>
      <PageHeader title="Dashboard">{actions}</PageHeader>
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
    </>
  );
}
