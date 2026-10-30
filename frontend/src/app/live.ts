import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { API_BASE } from "../api";
import type { Instance, LiveMessage } from "../api";
import { INSTANCES_KEY } from "../pages/devices/queries";

export type LiveState = "connecting" | "live" | "reconnecting";

const MIN_DELAY_MS = 1_000;
const MAX_DELAY_MS = 30_000;
const PING_MS = 25_000;

export function liveUrl(loc: Pick<Location, "protocol" | "host"> = window.location): string {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${loc.host}${API_BASE}/ws`;
}

/** Apply one push to the query cache. Unknown or malformed messages are ignored. */
/** The throughput card's live rate, as last pushed (query cache only, never fetched). */
export const LIVE_RATE_KEY = ["live", "rate"] as const;
export interface LiveRate {
  inBps: number;
  outBps: number;
  /** How many of the chosen ports the backend could read this time. */
  ports: number;
  receivedAt: number;
}

export function applyMessage(queryClient: QueryClient, raw: unknown): void {
  if (typeof raw !== "string") return;
  let msg: LiveMessage;
  try {
    msg = JSON.parse(raw) as LiveMessage;
  } catch {
    return;
  }
  if (msg.type === "instance_status") {
    queryClient.setQueryData<Instance[]>(INSTANCES_KEY, (old) =>
      old?.map((i) => (i.id === msg.instance_id ? { ...i, status: msg.status } : i)),
    );
    queryClient.setQueryData<Instance>([...INSTANCES_KEY, msg.instance_id], (old) =>
      old ? { ...old, status: msg.status } : old,
    );
  } else if (msg.type === "live_rate") {
    if (typeof msg.in_bps === "number" && typeof msg.out_bps === "number") {
      queryClient.setQueryData<LiveRate>(LIVE_RATE_KEY, {
        inBps: msg.in_bps,
        outBps: msg.out_bps,
        ports: typeof msg.ports === "number" ? msg.ports : 0,
        receivedAt: Date.now(),
      });
    }
  } else if (msg.type === "refresh") {
    // The list (status) and everything keyed under this instance.
    void queryClient.invalidateQueries({ queryKey: INSTANCES_KEY, exact: true });
    void queryClient.invalidateQueries({ queryKey: [...INSTANCES_KEY, msg.instance_id] });
  }
}

/**
 * One WebSocket for the whole app: updates the query cache from pushes and reconnects
 * with exponential backoff. The session is the httpOnly cookie; nothing is stored.
 */
export function connectLive(
  queryClient: QueryClient,
  onState: (state: LiveState) => void,
): () => void {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let ping: ReturnType<typeof setInterval> | undefined;
  let delay = MIN_DELAY_MS;
  let stopped = false;
  let opened = false;

  const open = () => {
    const ws = new WebSocket(liveUrl());
    socket = ws;
    ws.onopen = () => {
      delay = MIN_DELAY_MS;
      onState("live");
      // Pushes were missed while disconnected: refetch instead of trusting the cache.
      if (opened) void queryClient.invalidateQueries({ queryKey: INSTANCES_KEY });
      opened = true;
      ping = setInterval(() => ws.send("ping"), PING_MS);
    };
    ws.onmessage = (event) => applyMessage(queryClient, event.data);
    ws.onclose = () => {
      clearInterval(ping);
      if (stopped) return;
      onState("reconnecting");
      timer = setTimeout(open, delay);
      delay = Math.min(delay * 2, MAX_DELAY_MS);
    };
  };

  onState("connecting");
  open();
  return () => {
    stopped = true;
    clearTimeout(timer);
    clearInterval(ping);
    socket?.close();
  };
}

export function useLiveUpdates(queryClient: QueryClient): LiveState {
  const [state, setState] = useState<LiveState>("connecting");
  useEffect(() => connectLive(queryClient, setState), [queryClient]);
  return state;
}
