// Settings -> About (maintainer-approved 2026-11-13): what is running. Any logged-in user.
// The version is the server's release version (`GET /me`), with "Built as" when the images
// were built as another version (a final release promoted from its rc). A backend without
// `GET /about` (or with some fields null) shows "unknown" for those rows; a backend that
// reports no version shows the web app's own.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { useApiClient, useUser } from "../app/session";
import { formatDateTime } from "../app/time";
import {
  changelogUrl,
  commitsMismatch,
  FRONTEND_BUILD_DATE,
  FRONTEND_COMMIT,
  FRONTEND_VERSION,
  isKnownCommit,
  MISMATCH_TEXT,
  RELEASES_URL,
  shortCommit,
  shownVersion,
} from "../app/version";
import { PageHeader } from "../components/PageHeader";

/** "3 d 4 h", "2 h 5 min", "40 s". */
function uptime(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min`;
  return `${s} s`;
}

function Row({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4">
      <dt className="text-sm text-muted sm:w-48 sm:shrink-0">{name}</dt>
      <dd className="min-w-0 text-sm break-words">{children}</dd>
    </div>
  );
}

const linkClass = "font-medium underline";

function text(value: unknown): string {
  return typeof value === "string" && value !== "" ? value : "unknown";
}

/** A source revision, shortened (the full one on hover), or "unknown". */
function Revision({ value }: { value: string | null | undefined }) {
  if (!isKnownCommit(value)) return <>unknown</>;
  return (
    <span className="font-mono" title={value}>
      {shortCommit(value)}
    </span>
  );
}

export function AboutPage() {
  const api = useApiClient();
  const user = useUser();
  const [now] = useState(() => Date.now());
  // A 404 (a backend without /about) just leaves `data` empty.
  const about = useQuery({
    queryKey: ["about"],
    queryFn: () => api.about(),
    staleTime: 30_000,
    retry: false,
  });
  const data = about.data;
  const release = shownVersion(user.server_version, FRONTEND_VERSION);
  const builtAs = data?.backend_build_version;
  const serverCommit = user.server_commit ?? data?.backend_commit;
  const build = (date: string | null | undefined) =>
    typeof date === "string" && date !== "" ? ` (built ${date})` : "";
  const started = typeof data?.started_at === "string" ? Date.parse(data.started_at) : NaN;
  return (
    <>
      <PageHeader title="About" />
      {commitsMismatch(FRONTEND_COMMIT, serverCommit) && isKnownCommit(serverCommit) && (
        <p
          role="alert"
          className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
        >
          {MISMATCH_TEXT(FRONTEND_COMMIT, serverCommit)}
        </p>
      )}
      <dl className="divide-y divide-line">
        <Row name="Version">
          {release}
          {typeof builtAs === "string" && builtAs !== "" && builtAs !== release && (
            <span className="text-muted"> · Built as {builtAs}</span>
          )}
        </Row>
        <Row name="Server (backend)">
          <Revision value={serverCommit} />
          {build(data?.backend_build_date)}
        </Row>
        <Row name="Web app (frontend)">
          <Revision value={FRONTEND_COMMIT} />
          {build(FRONTEND_BUILD_DATE)}
        </Row>
        <Row name="Database schema">{text(data?.schema_version)}</Row>
        <Row name="Prometheus">{text(data?.prometheus_version)}</Row>
        <Row name="Running since">
          {Number.isNaN(started)
            ? "unknown"
            : `${formatDateTime(started)} (up ${uptime(now - started)})`}
        </Row>
        <Row name="Release notes">
          <a href={changelogUrl(release)} target="_blank" rel="noreferrer" className={linkClass}>
            Changelog for this version
          </a>
          {" · "}
          <a href={RELEASES_URL} target="_blank" rel="noreferrer" className={linkClass}>
            All releases
          </a>
        </Row>
      </dl>
    </>
  );
}
