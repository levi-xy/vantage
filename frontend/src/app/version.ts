// The versions shown in the UI. The web app's own comes from the image build ("dev" for
// local and CI builds) and never depends on the backend; the server's comes from
// `GET /me`. Neither is a secret.

export const FRONTEND_VERSION: string = __VANTAGE_VERSION__;
export const FRONTEND_BUILD_DATE: string = __VANTAGE_BUILD_DATE__;

export const RELEASES_URL = "https://github.com/levi-xy/vantage/releases";

/** "dev" (a local or CI build) and an empty value count as unknown. */
export function isKnownVersion(version: string | undefined): version is string {
  return version !== undefined && version !== "" && version !== "dev";
}

/** True only when both versions are known and differ. */
export function versionsMismatch(web: string, server: string | undefined): boolean {
  return isKnownVersion(web) && isKnownVersion(server) && web !== server;
}

export const MISMATCH_TEXT = (web: string, server: string) =>
  `The web app (${web}) and the server (${server}) are different versions. Reload the page; if it persists, pull both images again.`;

/**
 * This version's changelog, at its release tag. The headings carry a date
 * ("## 0.1.1 (2026-11-14)") that the browser can't know, so there is no section anchor:
 * the newest version is at the top of the file. Dev or unknown builds: the releases list.
 */
export function changelogUrl(version: string | undefined): string {
  if (!isKnownVersion(version) || !/^[0-9A-Za-z.+-]+$/.test(version)) return RELEASES_URL;
  return `https://github.com/levi-xy/vantage/blob/v${version}/CHANGELOG.md`;
}
