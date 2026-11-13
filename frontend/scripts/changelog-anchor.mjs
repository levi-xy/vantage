// The GitHub anchor of a version's section in CHANGELOG.md, for the About page's release
// notes link. Used by the release workflow (the frontend image's build context is
// frontend/ only, so the anchor is computed outside and passed in as a build arg):
//
//   node frontend/scripts/changelog-anchor.mjs CHANGELOG.md 0.1.1   ->  011-2026-11-20
//
// Prints nothing when there is no matching heading.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** GitHub's heading slug: lowercase; drop everything but letters, digits, spaces, hyphens, underscores; spaces to hyphens. */
export function slug(heading) {
  return heading
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\p{M} _-]/gu, "")
    .replace(/ /g, "-");
}

/** The anchor of the first `## ` heading holding `version` as a whole token, else "". */
export function changelogAnchor(text, version) {
  if (!version || version === "dev") return "";
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith("## ")) continue;
    const heading = line.slice(3);
    const tokens = heading.split(/[\s()]+/);
    if (tokens.includes(version)) return slug(heading);
  }
  return "";
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [file, version] = process.argv.slice(2);
  process.stdout.write(changelogAnchor(readFileSync(file, "utf8"), version ?? ""));
}
