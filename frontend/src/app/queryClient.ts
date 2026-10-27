import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api";

const MAX_RETRIES = 2;

/** Retry network failures and 5xx; a 4xx (401, 403, 404, 422...) won't change on retry. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < MAX_RETRIES;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, staleTime: 30_000, refetchOnWindowFocus: false },
      // Writes are never retried automatically.
      mutations: { retry: false },
    },
  });
}
