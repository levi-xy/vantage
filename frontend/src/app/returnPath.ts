/**
 * Where to go after logging in, from the router state the session gate recorded.
 * Only an in-app path is accepted; anything else (another origin, the login or setup
 * page itself, a malformed value) falls back to the dashboard.
 */
export function returnPath(state: unknown): string {
  if (typeof state !== "object" || state === null || !("from" in state)) return "/";
  const { from } = state;
  if (typeof from !== "string" || !from.startsWith("/") || from.startsWith("//")) return "/";
  if (from.includes("\\") || /^\/(login|setup)(?:[/?#]|$)/.test(from)) return "/";
  return from;
}
