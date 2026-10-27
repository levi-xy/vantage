import { ApiError } from "../api";
import { createQueryClient, shouldRetry } from "./queryClient";

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

  it("never retries mutations", () => {
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });
});
