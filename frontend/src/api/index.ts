import { ApiClient } from "./client";

export { ApiClient, ApiError, API_BASE, CSRF_HEADER } from "./client";
export type * from "./types";

/** The app-wide client. `SessionProvider` (app/session.tsx) listens for its 401s. */
export const api = new ApiClient();
