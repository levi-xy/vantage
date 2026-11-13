// The versions shown in the UI. The web app's own comes from the image build ("dev" for
// local and CI builds) and never depends on the backend; the server's comes from
// `GET /me`. Neither is a secret.

export const FRONTEND_VERSION: string = __VANTAGE_VERSION__;
export const FRONTEND_BUILD_DATE: string = __VANTAGE_BUILD_DATE__;
/** This build's section of CHANGELOG.md, computed by the release workflow ("" = none). */
export const CHANGELOG_ANCHOR: string = __CHANGELOG_ANCHOR__;

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
 * This version's changelog, at its release tag. The section anchor (the headings carry a
 * date the browser can't know) is computed at release time and only fits this web app's own
 * version. Dev or unknown builds: the releases list.
 */
export function changelogUrl(version: string | undefined): string {
  if (!isKnownVersion(version) || !/^[0-9A-Za-z.+-]+$/.test(version)) return RELEASES_URL;
  const anchor =
    version === FRONTEND_VERSION && /^[a-z0-9_-]+$/.test(CHANGELOG_ANCHOR)
      ? `#${CHANGELOG_ANCHOR}`
      : "";
  return `https://github.com/levi-xy/vantage/blob/v${version}/CHANGELOG.md${anchor}`;
}
