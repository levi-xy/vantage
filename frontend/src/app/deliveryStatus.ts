import { useQuery } from "@tanstack/react-query";
import { ALERTS_KEY } from "./live";
import { useApiClient } from "./session";

const DELIVERY_POLL_MS = 60_000;

/** System alerts and stuck notifications: one poll, shared by the banners and the status. */
export function useDeliveryStatus() {
  const api = useApiClient();
  return useQuery({
    queryKey: [...ALERTS_KEY, "delivery-status"],
    queryFn: () => api.alertDeliveryStatus(),
    refetchInterval: DELIVERY_POLL_MS,
  });
}
