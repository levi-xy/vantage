import { ApiClient } from "./client";

export { ApiClient, ApiError, API_BASE, CSRF_HEADER } from "./client";
export type * from "./types";

/** The app-wide client. Session handling (`onUnauthorized` → login) arrives with M3.2. */
export const api = new ApiClient();
