import { ApiError } from "../api";
import { createQueryClient, MAX_BUSY_RETRIES, retryDelay, shouldRetry } from "./queryClient";

describe("query retries", () => {
  it.each([401, 403, 404, 422])("does not retry a %i", (status) => {
    expect(shouldRetry(0, new ApiError(status, "no"))).toBe(false);
  });

  it("retries network failures and server errors twice", () => {
    for (const error of [new ApiError(0, "offline"), new ApiError(503, "down"), new Error("x")]) {
      expect(shouldRetry(0, error)).toBe(true);
      expect(shouldRetry(1, error)).toBe(true);
      expect(shouldRetry(2, error)).toBe(false);
    }
  });

  it("retries a 429 (busy) up to five times after a short random wait", () => {
    const busy = new ApiError(429, "too many queries in progress; try again");
    expect(MAX_BUSY_RETRIES).toBe(5);
    expect(shouldRetry(0, busy)).toBe(true);
    expect(shouldRetry(4, busy)).toBe(true);
    expect(shouldRetry(5, busy)).toBe(false);
    const delay = retryDelay(0, busy);
    expect(delay).toBeGreaterThanOrEqual(500);
    expect(delay).toBeLessThanOrEqual(1_500);
    expect(retryDelay(1, new ApiError(503, "down"))).toBe(2_000);
  });

  it("never retries mutations", () => {
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });
});
