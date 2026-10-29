import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import type { CardPlacement, Dashboard } from "../api";
import { useApiClient, useUser } from "../app/session";
import { DashboardGrid } from "../cards/DashboardGrid";
import { cardKey, mergeLayout } from "../cards/layout";
import type { AvailableCard } from "../cards/layout";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError } from "../components/styles";
import { errorText, useInstances } from "./devices/queries";

/** The one dashboard there is for now (the backend creates it on first read). */
const DASHBOARD_ID = 1;
const DASHBOARD_KEY = ["dashboards", DASHBOARD_ID] as const;

export function DashboardPage() {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const isAdmin = useUser().role === "admin";
  const instances = useInstances();
  const dashboard = useQuery({
    queryKey: DASHBOARD_KEY,
    queryFn: () => api.dashboard(DASHBOARD_ID),
  });

  const instanceList = useMemo(() => instances.data ?? [], [instances.data]);
  const cardQueries = useQueries({
    queries: instanceList.map((i) => ({
      queryKey: ["instances", i.id, "cards"],
      queryFn: () => api.instanceCards(i.id),
      // Card specs only change with a backend upgrade.
      staleTime: Infinity,
    })),
  });

  const save = useMutation({
    mutationFn: (cards: CardPlacement[]) => api.saveDashboard(DASHBOARD_ID, { cards }),
    // Show the new layout at once so the grid doesn't snap back while the request runs.
    onMutate: (cards) => {
      queryClient.setQueryData<Dashboard>(DASHBOARD_KEY, (old) =>
        old ? { ...old, layout: { cards } } : old,
      );
    },
    onError: () => queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY }),
  });

  // The saved layout plus the cards of instances it doesn't have yet.
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
  const merged =
    dashboard.data && instances.data
      ? mergeLayout(dashboard.data.layout.cards, available, known, loaded)
      : null;

  // Admins save the additions once; the key stops a failing save from retrying in a loop.
  const attempted = useRef("");
  const mergedJson = merged?.changed ? JSON.stringify(merged.cards) : "";
  const { mutate } = save;
  useEffect(() => {
    if (!isAdmin || !merged?.changed || mergedJson === attempted.current) return;
    attempted.current = mergedJson;
    mutate(merged.cards);
  }, [isAdmin, merged, mergedJson, mutate]);

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
  } else if (!instanceList.length) {
    body = <EmptyState>No cards yet. Add a device to get started.</EmptyState>;
  } else if (merged) {
    body = (
      <DashboardGrid
        cards={merged.cards}
        instances={new Map(instanceList.map((i) => [i.id, i]))}
        titles={titles}
        editable={isAdmin}
        onChange={(cards) => save.mutate(cards)}
      />
    );
  }

  return (
    <>
      <PageHeader title="Dashboard" />
      {save.isError && (
        <p role="alert" className={`mb-4 ${alertError}`}>
          {errorText(save.error)}
        </p>
      )}
      {body}
    </>
  );
}
