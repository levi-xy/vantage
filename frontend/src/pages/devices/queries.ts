import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../../api";
import { useApiClient } from "../../app/session";

/** A readable line for a failed request. API errors never carry secrets (backend rule). */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Only admins can change devices.";
    return error.message;
  }
  return "Something went wrong. Try again.";
}

// Status comes from the poll collector; refresh it until /ws pushes changes (later milestone).
const STATUS_REFRESH_MS = 30_000;

export const INSTANCES_KEY = ["instances"] as const;
export const INTEGRATIONS_KEY = ["integrations"] as const;

export function useInstances() {
  const api = useApiClient();
  return useQuery({
    queryKey: INSTANCES_KEY,
    queryFn: () => api.instances(),
    refetchInterval: STATUS_REFRESH_MS,
  });
}

export function useInstance(id: string) {
  const api = useApiClient();
  return useQuery({ queryKey: [...INSTANCES_KEY, id], queryFn: () => api.instance(id) });
}

export function useIntegrations() {
  const api = useApiClient();
  return useQuery({
    queryKey: INTEGRATIONS_KEY,
    queryFn: () => api.integrations(),
    // The registry only changes with a backend upgrade.
    staleTime: Infinity,
  });
}
