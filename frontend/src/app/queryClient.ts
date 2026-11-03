import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api";

const MAX_RETRIES = 2;
/** A 429 clears as soon as a slot frees up, so it gets a budget of its own. */
export const MAX_BUSY_RETRIES = 5;

const isBusy = (error: unknown) => error instanceof ApiError && error.status === 429;

/**
 * Retry network failures and 5xx (twice) and 429 (too many queries running at once: up
 * to 5 times, shortly); any other 4xx (401, 403, 404, 422...) won't change on retry.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500 && !isBusy(error)) {
    return false;
  }
  return failureCount < (isBusy(error) ? MAX_BUSY_RETRIES : MAX_RETRIES);
}

/** TanStack's default backoff; a 429 waits a random 0.5-1.5 s so retries don't collide. */
export function retryDelay(failureCount: number, error: unknown): number {
  if (isBusy(error)) return 500 + Math.random() * 1_000;
  return Math.min(1_000 * 2 ** failureCount, 30_000);
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
      // Writes are never retried automatically.
      mutations: { retry: false },
    },
  });
}
