import { useQuery } from "@tanstack/react-query";
import type { ApiClient, DashboardSummary } from "../api";
import { addCard, cardKey } from "../cards/layout";
import { cardFullSize } from "../cards/widgets";
import { useApiClient } from "../app/session";

/** Dashboards are global: anyone logged in can add, rename, move, edit and delete them. */
export const DASHBOARDS_KEY = ["dashboards", "list"] as const;
export const dashboardKey = (id: number) => ["dashboards", id] as const;

/** Dashboard 1 is created by the backend on its first read when there is no dashboard. */
export const DEFAULT_DASHBOARD_ID = 1;

export function useDashboards() {
  const api = useApiClient();
  return useQuery({ queryKey: DASHBOARDS_KEY, queryFn: () => api.dashboards() });
}

/** The main dashboard (the first one) lives at `/`, every other one at `/d/<id>`. */
export function dashboardPath(list: DashboardSummary[], id: number): string {
  return list[0]?.id === id ? "/" : `/d/${id}`;
}

/**
 * Put a new device's cards on the main dashboard (the first one), at the bottom, each at
 * its full size; cards already there are skipped. Best effort: the device is already saved,
 * so a failure here only means the admin adds the cards by hand in edit mode.
 */
export async function addDeviceCardsToMain(api: ApiClient, instanceId: string): Promise<void> {
  const [specs, list] = await Promise.all([api.instanceCards(instanceId), api.dashboards()]);
  const main = list[0];
  if (!main || specs.length === 0) return;
  const { layout } = await api.dashboard(main.id);
  const placed = new Set(layout.cards.map(cardKey));
  let cards = layout.cards;
  for (const spec of specs) {
    const card = { instance_id: instanceId, card_type: spec.card_type };
    if (placed.has(cardKey(card))) continue;
    const [fullW, fullH] = cardFullSize(spec.card_type, instanceId);
    cards = addCard(cards, card, [
      Math.max(spec.default_size[0], fullW),
      Math.max(spec.default_size[1], fullH),
    ]);
  }
  if (cards === layout.cards) return;
  // The saved version is kept, so an old layout still gets its one-time upgrade on display.
  await api.saveDashboard(main.id, { ...layout, cards });
}
