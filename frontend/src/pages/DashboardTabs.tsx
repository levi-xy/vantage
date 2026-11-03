import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { useApiClient } from "../app/session";
import { alertError, buttonPrimary, buttonSecondary, input } from "../components/styles";
import { DASHBOARDS_KEY, dashboardKey, dashboardPath, useDashboards } from "./dashboards";
import { errorText } from "./devices/queries";

const NAME_MAX = 64;

const tabClass = (active: boolean) =>
  `-mb-px border-b-2 px-3 py-1.5 whitespace-nowrap focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none focus-visible:ring-inset ${
    active ? "border-in font-medium text-ink" : "border-transparent text-muted hover:text-ink"
  }`;

function NameForm({
  label,
  initial,
  submitLabel,
  copyOf,
  busy,
  onSubmit,
  onCancel,
}: {
  label: string;
  initial: string;
  submitLabel: string;
  /** Offer "Copy of <name>" (new dashboards only). */
  copyOf?: string;
  busy: boolean;
  onSubmit: (name: string, copy: boolean) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial);
  const [copy, setCopy] = useState(false);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim()) onSubmit(name.trim(), copy);
  };
  return (
    <form
      aria-label={label}
      onSubmit={submit}
      className="flex flex-wrap items-center gap-2 py-2 text-sm"
    >
      <label className="flex items-center gap-2">
        <span>Name</span>
        <input
          className={`${input} w-56`}
          value={name}
          maxLength={NAME_MAX}
          required
          autoFocus
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {copyOf !== undefined && (
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={copy} onChange={(e) => setCopy(e.target.checked)} />
          Copy of {copyOf}
        </label>
      )}
      <button type="submit" className={buttonPrimary} disabled={busy || !name.trim()}>
        {submitLabel}
      </button>
      <button type="button" className={buttonSecondary} onClick={onCancel}>
        Cancel
      </button>
    </form>
  );
}

/**
 * The dashboards as tabs above the cards (a select on phones), a "+" to add one, and in
 * edit mode rename, move and delete for the current one. Each tab is a real link, so a
 * dashboard can be opened (and edited) in another browser tab.
 */
export function DashboardTabs({
  current,
  currentName,
  editing,
  atMain,
}: {
  /** The dashboard shown; null when the address names none. */
  current: number | null;
  currentName: string | undefined;
  editing: boolean;
  /** Shown at `/` (as the main dashboard) rather than at `/d/<id>`. */
  atMain: boolean;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const dashboards = useDashboards();
  const [form, setForm] = useState<"new" | "rename" | null>(null);
  const list = dashboards.data ?? [];
  const index = list.findIndex((d) => d.id === current);

  const refreshList = () => queryClient.invalidateQueries({ queryKey: DASHBOARDS_KEY });
  const create = useMutation({
    mutationFn: ({ name, copy }: { name: string; copy: boolean }) =>
      api.createDashboard(copy && current !== null ? { name, copy_from: current } : { name }),
    onSuccess: async (created) => {
      queryClient.setQueryData(dashboardKey(created.id), created);
      setForm(null);
      await refreshList();
      void navigate(`/d/${created.id}`);
    },
  });
  const update = useMutation({
    mutationFn: (body: { name?: string; position?: number }) =>
      api.updateDashboard(current ?? 0, body),
    onSuccess: async (_, body) => {
      setForm(null);
      if (current === null) return;
      await queryClient.invalidateQueries({ queryKey: dashboardKey(current) });
      await refreshList();
      // The main dashboard moved away from the front keeps showing (and keeps its draft).
      if (atMain && body.position !== undefined) {
        void navigate(`/d/${current}`, { replace: true });
      }
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteDashboard(current ?? 0),
    onSuccess: async () => {
      if (current !== null) queryClient.removeQueries({ queryKey: dashboardKey(current) });
      await refreshList();
      void navigate("/");
    },
  });
  const failed = create.error ?? update.error ?? remove.error;

  const onDelete = () => {
    if (window.confirm(`Delete the dashboard "${currentName ?? ""}"? Its layout is lost.`)) {
      remove.mutate();
    }
  };

  return (
    <div className="mb-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 border-b border-line">
        <nav aria-label="Dashboards" className="hidden flex-wrap md:flex">
          {list.map((d) => (
            <Link
              key={d.id}
              to={dashboardPath(list, d.id)}
              aria-current={d.id === current ? "page" : undefined}
              className={tabClass(d.id === current)}
            >
              {d.name}
            </Link>
          ))}
        </nav>
        {list.length > 0 && (
          <label className="flex items-center gap-2 pb-2 text-sm md:hidden">
            <span className="text-muted">Dashboard</span>
            <select
              className={`${input} w-auto`}
              value={index >= 0 ? String(current) : ""}
              onChange={(e) => void navigate(dashboardPath(list, Number(e.target.value)))}
            >
              {index < 0 && <option value="">Choose…</option>}
              {list.map((d) => (
                <option key={d.id} value={String(d.id)}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="button"
          aria-label="New dashboard"
          title="New dashboard"
          className={`${tabClass(false)} text-lg leading-none`}
          onClick={() => setForm(form === "new" ? null : "new")}
        >
          +
        </button>
        {editing && index >= 0 && (
          <div className="ml-auto flex gap-2 pb-2">
            <button type="button" className={buttonSecondary} onClick={() => setForm("rename")}>
              Rename
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={index === 0 || update.isPending}
              onClick={() => update.mutate({ position: index - 1 })}
            >
              Move left
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={index === list.length - 1 || update.isPending}
              onClick={() => update.mutate({ position: index + 1 })}
            >
              Move right
            </button>
            <button
              type="button"
              className={buttonSecondary}
              disabled={list.length <= 1 || remove.isPending}
              onClick={onDelete}
            >
              Delete
            </button>
          </div>
        )}
      </div>
      {form === "new" && (
        <NameForm
          label="New dashboard"
          initial=""
          submitLabel="Create"
          copyOf={current !== null ? currentName : undefined}
          busy={create.isPending}
          onSubmit={(name, copy) => create.mutate({ name, copy })}
          onCancel={() => setForm(null)}
        />
      )}
      {form === "rename" && editing && (
        <NameForm
          label="Rename dashboard"
          initial={currentName ?? ""}
          submitLabel="Save"
          busy={update.isPending}
          onSubmit={(name) => update.mutate({ name })}
          onCancel={() => setForm(null)}
        />
      )}
      {failed && (
        <p role="alert" className={`mt-2 ${alertError}`}>
          {errorText(failed)}
        </p>
      )}
    </div>
  );
}
