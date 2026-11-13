// The versions shown in the UI (maintainer-approved 2026-11-13). The UI shows the
// server's release version (`GET /me` server_version): a final release promotes its rc's
// images unchanged, so only the server knows the release (VANTAGE_RELEASE in the compose
// file). The web app's own baked version is the fallback for a server that reports none.
// A mismatch is judged by commit: an rc and its promoted final share theirs. None of
// these is a secret.

export const FRONTEND_VERSION: string = __VANTAGE_VERSION__;
export const FRONTEND_BUILD_DATE: string = __VANTAGE_BUILD_DATE__;
export const FRONTEND_COMMIT: string = __VANTAGE_COMMIT__;

export const RELEASES_URL = "https://github.com/levi-xy/vantage/releases";

/** "dev" (a local or CI build) and an empty value count as unknown. */
export function isKnownVersion(version: string | null | undefined): version is string {
  return typeof version === "string" && version !== "" && version !== "dev";
}

/** A missing, empty or "dev" commit is unknown. */
export function isKnownCommit(commit: string | null | undefined): commit is string {
  return typeof commit === "string" && commit !== "" && commit !== "dev";
}

/** The version to show: the server's, or this web app's own when the server reports none. */
export function shownVersion(server: string | null | undefined, web: string): string {
  return typeof server === "string" && server !== "" ? server : web;
}

/** True only when both commits are known and differ. */
export function commitsMismatch(web: string, server: string | null | undefined): boolean {
  return isKnownCommit(web) && isKnownCommit(server) && web !== server;
}

/** The first 12 characters, enough to find the commit. */
export function shortCommit(commit: string): string {
  return commit.slice(0, 12);
}

export const MISMATCH_TEXT = (web: string, server: string) =>
  `The web app (commit ${shortCommit(web)}) and the server (commit ${shortCommit(server)}) are different builds. Reload the page; if it persists, pull both images again.`;

/**
 * The release's changelog: CHANGELOG.md at the release tag. It lists the newest version
 * first, so the file at tag vX.Y.Z opens on X.Y.Z's section; no anchor is needed (the
 * headings carry a release date no build can know in advance). Dev or unknown versions:
 * the releases list.
 */
export function changelogUrl(version: string | null | undefined): string {
  if (!isKnownVersion(version) || !/^[0-9A-Za-z.+-]+$/.test(version)) return RELEASES_URL;
  return `https://github.com/levi-xy/vantage/blob/v${version}/CHANGELOG.md`;
}
