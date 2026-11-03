// Admin only: rules (list, create, edit, enable/disable, delete) and templates.
// The backend validates the expression; its refusal text is shown as is.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../../api";
import type { AlertRule, AlertRuleInput, AlertSeverity, AlertTemplate } from "../../api";
import { useApiClient } from "../../app/session";
import { EmptyState } from "../../components/PageHeader";
import {
  alertError,
  buttonPrimary,
  buttonSecondary,
  fieldHint,
  input,
  label,
} from "../../components/styles";
import { INSTANCES_KEY } from "../devices/queries";
import { buttonDanger, panel, row } from "./tone";
import { SectionHeader } from "./ui";

const RULES_KEY = ["alert-rules"] as const;
const CHANNELS_KEY = ["channels"] as const;

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Only admins can manage alert rules.";
    if (error.status === 422) return `The rule was refused: ${error.message}`;
    return error.message;
  }
  return "Something went wrong. Try again.";
}

const EMPTY: AlertRuleInput = {
  name: "",
  expr: "",
  for_seconds: 0,
  severity: "warning",
  instance_id: null,
  channel_ids: [],
  enabled: true,
};

function RuleForm({
  rule,
  initial,
  onDone,
}: {
  rule?: AlertRule;
  initial?: AlertRuleInput;
  onDone: () => void;
}) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const start = rule ?? initial ?? EMPTY;
  const prefix = rule ? `rule-${rule.id}` : "new-rule";
  const [name, setName] = useState(start.name);
  const [expr, setExpr] = useState(start.expr);
  const [forSeconds, setForSeconds] = useState(String(start.for_seconds));
  const [severity, setSeverity] = useState<AlertSeverity>(start.severity);
  const [instanceId, setInstanceId] = useState(start.instance_id ?? "");
  const [channelIds, setChannelIds] = useState<number[]>(start.channel_ids);
  const [enabled, setEnabled] = useState(start.enabled);
  const [error, setError] = useState<string | null>(null);
  const instances = useQuery({ queryKey: INSTANCES_KEY, queryFn: () => api.instances() });
  const channels = useQuery({ queryKey: CHANNELS_KEY, queryFn: () => api.channels() });

  const save = useMutation({
    mutationFn: () => {
      const body: AlertRuleInput = {
        name: name.trim(),
        expr: expr.trim(),
        for_seconds: Number(forSeconds),
        severity,
        instance_id: instanceId || null,
        channel_ids: channelIds,
        enabled,
      };
      return rule ? api.updateAlertRule(rule.id, body) : api.createAlertRule(body);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: RULES_KEY });
      onDone();
    },
    onError: (e) => setError(errorText(e)),
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (save.isPending) return;
    const secs = Number(forSeconds);
    if (!name.trim() || !expr.trim()) {
      setError("Name and expression are required.");
      return;
    }
    if (!Number.isInteger(secs) || secs < 0 || secs > 86_400) {
      setError("For must be a whole number of seconds from 0 to 86400.");
      return;
    }
    setError(null);
    save.mutate();
  }

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      aria-label={rule ? `Edit rule ${rule.name}` : "Add a rule"}
      className="grid gap-4 md:grid-cols-2"
    >
      <div>
        <label htmlFor={`${prefix}-name`} className={label}>
          Rule name
        </label>
        <input
          id={`${prefix}-name`}
          maxLength={128}
          className={input}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={`${prefix}-severity`} className={label}>
          Severity
        </label>
        <select
          id={`${prefix}-severity`}
          className={input}
          value={severity}
          onChange={(e) => setSeverity(e.target.value as AlertSeverity)}
        >
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
        </select>
      </div>
      <div className="md:col-span-2">
        <label htmlFor={`${prefix}-expr`} className={label}>
          Expression (PromQL)
        </label>
        <textarea
          id={`${prefix}-expr`}
          rows={3}
          spellCheck={false}
          className={`${input} font-mono`}
          value={expr}
          onChange={(e) => setExpr(e.target.value)}
        />
        <p className={fieldHint}>The rule fires while this returns any series.</p>
      </div>
      <div>
        <label htmlFor={`${prefix}-for`} className={label}>
          For (seconds)
        </label>
        <input
          id={`${prefix}-for`}
          inputMode="numeric"
          className={input}
          value={forSeconds}
          onChange={(e) => setForSeconds(e.target.value)}
        />
        <p className={fieldHint}>How long it must hold before the alert fires.</p>
      </div>
      <div>
        <label htmlFor={`${prefix}-instance`} className={label}>
          Device
        </label>
        <select
          id={`${prefix}-instance`}
          className={input}
          value={instanceId}
          onChange={(e) => setInstanceId(e.target.value)}
        >
          <option value="">All devices</option>
          {(instances.data ?? []).map((i) => (
            <option key={i.id} value={i.id}>
              {i.name}
            </option>
          ))}
        </select>
      </div>
      <fieldset className="md:col-span-2">
        <legend className={label}>Send to</legend>
        {(channels.data ?? []).length === 0 ? (
          <p className={fieldHint}>No channels yet. Add one below.</p>
        ) : (
          <div className="flex flex-wrap gap-4">
            {(channels.data ?? []).map((c) => (
              <label key={c.id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={channelIds.includes(c.id)}
                  onChange={(e) =>
                    setChannelIds((ids) =>
                      e.target.checked ? [...ids, c.id] : ids.filter((x) => x !== c.id),
                    )
                  }
                />
                {c.name}
              </label>
            ))}
          </div>
        )}
      </fieldset>
      <label className="flex items-center gap-2 text-sm md:col-span-2">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled
      </label>
      <div className="flex flex-col gap-3 md:col-span-2">
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="submit" disabled={save.isPending} className={buttonPrimary}>
            {save.isPending ? "Saving…" : rule ? "Save" : "Add rule"}
          </button>
          <button type="button" onClick={onDone} className={buttonSecondary}>
            Cancel
          </button>
        </div>
      </div>
    </form>
  );
}

function RuleRow({ rule }: { rule: AlertRule }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"idle" | "edit" | "confirm-delete">("idle");
  const [error, setError] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: RULES_KEY });
  const toggle = useMutation({
    mutationFn: () => api.updateAlertRule(rule.id, { enabled: !rule.enabled }),
    onSuccess: refresh,
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAlertRule(rule.id),
    onSuccess: refresh,
    onError: (e) => {
      setMode("idle");
      setError(errorText(e));
    },
  });
  return (
    <li className={row}>
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className={`flex min-w-0 flex-col gap-1 ${rule.enabled ? "" : "text-muted"}`}>
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-semibold break-all">{rule.name}</span>
            <span className="text-sm text-muted tabular-nums">
              {rule.severity} · for {rule.for_seconds}s{rule.enabled ? "" : " · disabled"}
            </span>
          </div>
          <code className="rounded bg-inset px-2 py-1 font-mono text-xs break-all text-muted">
            {rule.expr}
          </code>
          {rule.recovery_mode === "empty_result" && (
            <span className="text-xs text-warn">
              Resolves when the result is empty: this expression can&apos;t be split into a value
              and a threshold, so a missing value can&apos;t be told apart from a recovery (a
              device&apos;s fresh poll is still required).
            </span>
          )}
          {rule.error && (
            <span role="status" className="text-xs break-words text-crit-text">
              Last query error: {rule.error}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-2 md:justify-end">
          {mode === "confirm-delete" ? (
            <>
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => remove.mutate()}
                className={buttonDanger}
                aria-label={`Confirm deleting ${rule.name}`}
              >
                Confirm delete
              </button>
              <button type="button" onClick={() => setMode("idle")} className={buttonSecondary}>
                Keep
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={toggle.isPending}
                onClick={() => {
                  setError(null);
                  toggle.mutate();
                }}
                className={buttonSecondary}
                aria-label={`${rule.enabled ? "Disable" : "Enable"} ${rule.name}`}
              >
                {rule.enabled ? "Disable" : "Enable"}
              </button>
              <button
                type="button"
                onClick={() => setMode(mode === "edit" ? "idle" : "edit")}
                className={buttonSecondary}
                aria-label={`Edit ${rule.name}`}
                aria-expanded={mode === "edit"}
              >
                Edit
              </button>
              <button
                type="button"
                onClick={() => setMode("confirm-delete")}
                className={buttonSecondary}
                aria-label={`Delete ${rule.name}`}
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {mode === "edit" && (
        <div className="mt-3 border-t border-line pt-3">
          <RuleForm rule={rule} onDone={() => setMode("idle")} />
        </div>
      )}
      {error && (
        <p role="alert" className={`${alertError} mt-3`}>
          {error}
        </p>
      )}
    </li>
  );
}

export function RulesSection() {
  const api = useApiClient();
  const rules = useQuery({ queryKey: RULES_KEY, queryFn: () => api.alertRules() });
  const templates = useQuery({
    queryKey: ["alert-templates"],
    queryFn: () => api.alertTemplates(),
  });
  // `key` remounts the add form so a new template replaces what was typed.
  const formKey = useRef(0);
  const [adding, setAdding] = useState<{ key: number; initial?: AlertRuleInput } | null>(null);
  // 20+ templates: collapsed until asked for, so the rules stay in view.
  const [showTemplates, setShowTemplates] = useState(false);

  function fromTemplate(t: AlertTemplate) {
    setAdding({
      key: ++formKey.current,
      initial: {
        ...EMPTY,
        name: t.name,
        expr: t.expr,
        for_seconds: t.for_seconds,
        severity: t.severity,
      },
    });
  }

  let list;
  if (rules.isPending) {
    list = (
      <p role="status" className="text-sm text-muted">
        Loading rules…
      </p>
    );
  } else if (rules.isError) {
    list = (
      <p role="alert" className={alertError}>
        Could not load the rules: {errorText(rules.error)}
      </p>
    );
  } else if (rules.data.length === 0) {
    list = <EmptyState>No rules yet. Add one or start from a template.</EmptyState>;
  } else {
    list = (
      <ul aria-label="Rules" className={panel}>
        {rules.data.map((r) => (
          <RuleRow key={r.id} rule={r} />
        ))}
      </ul>
    );
  }
  const templateList = templates.data ?? [];

  return (
    <>
      <section aria-labelledby="rules-title" className="space-y-2.5">
        <SectionHeader id="rules-title" title="Rules" count={rules.data?.length}>
          <button
            type="button"
            className={buttonSecondary}
            onClick={() => setAdding({ key: ++formKey.current })}
          >
            Add a rule
          </button>
        </SectionHeader>
        {list}
        {adding && (
          <div className={`${panel} p-4`}>
            <RuleForm key={adding.key} initial={adding.initial} onDone={() => setAdding(null)} />
          </div>
        )}
      </section>
      <section aria-labelledby="templates-title" className="space-y-2.5">
        <h2 id="templates-title" className="text-[15px] font-semibold">
          <button
            type="button"
            aria-expanded={showTemplates}
            aria-controls="templates-list"
            onClick={() => setShowTemplates((o) => !o)}
            className="inline-flex items-center gap-1.5 rounded hover:underline focus-visible:ring-2 focus-visible:ring-in focus-visible:outline-none"
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.4"
              aria-hidden="true"
              className={showTemplates ? "rotate-90" : ""}
            >
              <path d="M9 6l6 6-6 6" />
            </svg>
            Create from template
            {templates.data && (
              <span className="font-normal text-muted tabular-nums">
                {" "}
                · {templates.data.length}
              </span>
            )}
          </button>
        </h2>
        {!showTemplates ? null : templates.isError ? (
          <p role="alert" className={alertError}>
            Could not load the templates.
          </p>
        ) : (
          templateList.length > 0 && (
            <ul id="templates-list" aria-label="Templates" className={panel}>
              {templateList.map((t) => (
                <li key={t.id} className={`${row} flex items-center justify-between gap-3`}>
                  <div className="min-w-0">
                    <p className="font-semibold break-words">{t.name}</p>
                    <p className="text-sm text-muted">{t.description}</p>
                  </div>
                  <button
                    type="button"
                    className={buttonSecondary}
                    aria-label={`Use template ${t.name}`}
                    onClick={() => fromTemplate(t)}
                  >
                    Use
                  </button>
                </li>
              ))}
            </ul>
          )
        )}
      </section>
    </>
  );
}
