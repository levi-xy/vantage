import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api";

const MAX_RETRIES = 2;

const isBusy = (error: unknown) => error instanceof ApiError && error.status === 429;

/**
 * Retry network failures, 5xx and 429 (too many queries running at once: try again
 * shortly); any other 4xx (401, 403, 404, 422...) won't change on retry.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500 && !isBusy(error)) {
    return false;
  }
  return failureCount < MAX_RETRIES;
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
