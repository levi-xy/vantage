// Admin-only audit log (M3.4). The route is guarded by <RequireRole role="admin">, and the
// backend enforces the role on GET /audit itself.
//
// `changes` arrives already redacted: every secret field holds the literal "<changed>".
// It is rendered as plain text through React (never as HTML), so that placeholder shows
// exactly as sent, and nothing in an entry can inject markup.

import { useInfiniteQuery } from "@tanstack/react-query";
import type { AuditEntry } from "../api";
import { useApiClient } from "../app/session";
import { EmptyState, PageHeader } from "../components/PageHeader";
import { alertError, buttonSecondary } from "../components/styles";
import { changeLines } from "./auditChanges";

const AUDIT_PAGE_SIZE = 50;

function Changes({ changes }: { changes: Record<string, unknown> }) {
  const lines = changeLines(changes);
  if (lines.length === 0) return <span className="text-slate-500 dark:text-slate-400">—</span>;
  return (
    <dl className="space-y-0.5 font-mono text-xs">
      {lines.map((line, i) => (
        // A literal "a.b" key and a nested a → b could share a path; the index keeps keys unique.
        <div key={`${i}:${line.path}`} className="break-words">
          <dt className="inline text-slate-500 dark:text-slate-400">{line.path}: </dt>
          <dd className="inline">{line.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Who acted: a user (through the API), the admin CLI on the server, or the backend. */
function actor(entry: AuditEntry): string {
  if (entry.actor_type === "cli") return "Admin CLI";
  if (entry.actor_type === "system") return "System";
  return entry.user_id === null ? "—" : `User #${entry.user_id}`;
}

function Row({ entry }: { entry: AuditEntry }) {
  const cell = "px-3 py-2 align-top";
  return (
    <tr className="border-t border-slate-200 dark:border-slate-800">
      <td className={`${cell} whitespace-nowrap`}>
        <time dateTime={entry.at}>{new Date(entry.at).toLocaleString()}</time>
      </td>
      <td className={`${cell} whitespace-nowrap`}>{actor(entry)}</td>
      <td className={`${cell} whitespace-nowrap font-medium`}>{entry.action}</td>
      <td className={cell}>
        <span className="whitespace-nowrap">{entry.target_type}</span>
        {entry.target_id !== null && (
          <span className="block font-mono text-xs break-all text-slate-500 dark:text-slate-400">
            {entry.target_id}
          </span>
        )}
      </td>
      <td className={`${cell} min-w-56`}>
        <Changes changes={entry.changes} />
      </td>
    </tr>
  );
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Could not load the audit log.";
}

export function AuditPage() {
  const client = useApiClient();
  const query = useInfiniteQuery({
    queryKey: ["audit"],
    queryFn: ({ pageParam }) => client.audit({ limit: AUDIT_PAGE_SIZE, before_id: pageParam }),
    initialPageParam: undefined as number | undefined,
    // A full page may have more behind it; keyset paging continues below its last id.
    getNextPageParam: (last) =>
      last.length === AUDIT_PAGE_SIZE ? last[last.length - 1]?.id : undefined,
    // Always show the latest entries when the page is opened.
    staleTime: 0,
  });

  const entries = query.data?.pages.flat() ?? [];

  let body;
  if (query.isPending) {
    body = (
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading audit log…
      </p>
    );
  } else if (query.isError && entries.length === 0) {
    body = (
      <div role="alert" className={`${alertError} flex items-center justify-between gap-3`}>
        <span>Could not load the audit log: {errorText(query.error)}</span>
        <button type="button" className={buttonSecondary} onClick={() => void query.refetch()}>
          Try again
        </button>
      </div>
    );
  } else if (entries.length === 0) {
    body = <EmptyState>No audit entries yet.</EmptyState>;
  } else {
    body = (
      <>
        <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Audit entries, newest first</caption>
            <thead className="bg-slate-50 text-xs text-slate-600 uppercase dark:bg-slate-900 dark:text-slate-400">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  Time
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  User
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Action
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Target
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Changes
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <Row key={entry.id} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
        {query.isFetchNextPageError && (
          <p role="alert" className={`${alertError} mt-3`}>
            Could not load older entries: {errorText(query.error)}
          </p>
        )}
        {query.hasNextPage && (
          <div className="mt-4">
            <button
              type="button"
              className={buttonSecondary}
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {query.isFetchingNextPage ? "Loading…" : "Load older entries"}
            </button>
          </div>
        )}
      </>
    );
  }

  return (
    <>
      <PageHeader title="Audit log" />
      {body}
    </>
  );
}
