import { useQuery } from "@tanstack/react-query";
import type { DashboardSummary } from "../api";
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
