#!/usr/bin/env node
// Fetches the dashboard-icons library (homarr-labs/dashboard-icons, Apache-2.0) for the map
// editor's icon picker, at the commit pinned in icons.manifest.json.
//
//   node scripts/fetch-icons.mjs            download every listed file, verify its SHA-256
//                                           against the manifest, write public/icons/di/
//   node scripts/fetch-icons.mjs --update <sha>
//                                           pin another commit: list its svg/ folder through
//                                           the GitHub API, download the files and rewrite
//                                           the manifest (review the diff, commit it)
//
// The files are served by Vantage's own web server; the browser never contacts a CDN
// (AGENTS.md hard rule 4). Files come from jsDelivr, or GitHub raw where jsDelivr refuses
// one; either way each is checked against the manifest. A missing or mismatching file fails the build. public/icons/di/
// is git-ignored: the app and its tests work without it (the picker shows an empty library,
// a box falls back to its generic icon).

import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = "homarr-labs/dashboard-icons";
const CDN = `https://cdn.jsdelivr.net/gh/${REPO}@`;
const RAW = `https://raw.githubusercontent.com/${REPO}/`;
const API = `https://api.github.com/repos/${REPO}/git/trees/`;
/** The slug the map document accepts (backend `IconRef`); other file names are skipped. */
const SLUG = /^[a-z0-9-]{1,64}$/;
const PARALLEL = 8;

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifestPath = join(root, "icons.manifest.json");
const outDir = join(root, "public", "icons", "di");

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

/** One file at the pinned commit: jsDelivr first, GitHub raw for the few it refuses. */
async function getFile(commit, path) {
  try {
    return await get(`${CDN}${commit}/${path}`, "bytes", 1);
  } catch {
    return await get(`${RAW}${commit}/${path}`);
  }
}

async function get(url, as = "bytes", tries = 4) {
  let last;
  for (let attempt = 1; attempt <= tries; attempt++) {
    try {
      const res = await fetch(url, {
        signal: AbortSignal.timeout(60_000),
        headers: { "user-agent": "vantage-fetch-icons" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return as === "json" ? await res.json() : new Uint8Array(await res.arrayBuffer());
    } catch (err) {
      last = err;
      await new Promise((r) => setTimeout(r, attempt * 500));
    }
  }
  throw new Error(`${url}: ${last instanceof Error ? last.message : last}`);
}

/** Run `fn` over `items`, a few at a time. */
async function pool(items, fn) {
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const item = items[next++];
      await fn(item);
    }
  };
  await Promise.all(Array.from({ length: PARALLEL }, worker));
}

async function update(commit) {
  if (!/^[0-9a-f]{40}$/.test(commit ?? "")) {
    throw new Error("usage: fetch-icons.mjs --update <40-character commit sha>");
  }
  const tree = await get(`${API}${commit}?recursive=1`, "json");
  if (tree.truncated) throw new Error("the GitHub tree answer was truncated");
  const paths = tree.tree
    .filter((e) => e.type === "blob" && /^svg\/[^/]+\.svg$/.test(e.path))
    .map((e) => e.path)
    .filter((p) => SLUG.test(p.slice(4, -4)));
  paths.push("metadata.json");
  const files = {};
  await pool(paths, async (path) => {
    files[path] = sha256(await getFile(commit, path));
  });
  const sorted = Object.fromEntries(Object.entries(files).sort(([a], [b]) => (a < b ? -1 : 1)));
  await writeFile(manifestPath, `${JSON.stringify({ commit, files: sorted }, null, 2)}\n`);
  console.log(`manifest updated: ${paths.length} files at ${commit}`);
}

async function install() {
  const { commit, files } = JSON.parse(await readFile(manifestPath, "utf8"));
  const paths = Object.keys(files);
  if (!/^[0-9a-f]{40}$/.test(commit) || paths.length === 0) {
    throw new Error("icons.manifest.json is empty; run with --update <sha> first");
  }
  await mkdir(outDir, { recursive: true });
  let metadata = null;
  await pool(paths, async (path) => {
    const bytes = await getFile(commit, path);
    if (sha256(bytes) !== files[path])
      throw new Error(`${path}: checksum differs from the manifest`);
    if (path === "metadata.json") metadata = JSON.parse(new TextDecoder().decode(bytes));
    else await writeFile(join(outDir, path.slice(4)), bytes);
  });
  const have = new Set(paths.filter((p) => p !== "metadata.json").map((p) => p.slice(4, -4)));
  const index = [...have].sort().map((slug) => ({
    slug,
    name: slug.replaceAll("-", " "),
    aliases: (metadata?.[slug]?.aliases ?? []).filter((a) => typeof a === "string").slice(0, 8),
  }));
  await writeFile(join(outDir, "index.json"), JSON.stringify(index));
  console.log(`icons: ${have.size} files written to ${outDir}`);
}

const args = process.argv.slice(2);
try {
  if (args[0] === "--update") await update(args[1]);
  else if (args.length === 0) await install();
  else throw new Error("usage: fetch-icons.mjs [--update <sha>]");
} catch (err) {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
}
