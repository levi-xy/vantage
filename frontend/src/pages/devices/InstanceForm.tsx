import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router";
import type { Instance, IntegrationInfo, TestResult } from "../../api";
import { useApiClient } from "../../app/session";
import {
  alertError,
  alertInfo,
  buttonPrimary,
  buttonSecondary,
  fieldError,
  fieldHint,
  input,
  label,
} from "../../components/styles";
import { ConfigFields } from "./ConfigFields";
import { errorText } from "./queries";
import { buildConfig, configChanged, configFields, initialValues } from "./schemaForm";
import type { FieldValue } from "./schemaForm";

const NAME_MAX = 128;
const SITE_MAX = 64;
const TAG_MAX = 64;
const TAGS_MAX = 32;

function parseTags(text: string): string[] {
  const tags = text
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  return [...new Set(tags)];
}

/** Connection test outcome, with the firewall hint when it failed. */
export function TestOutcome({ result }: { result: TestResult }) {
  if (result.ok) {
    return (
      <div
        role="status"
        className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-100"
      >
        Connection OK: {result.message}
      </div>
    );
  }
  const hint = result.hint;
  return (
    <div role="alert" className={alertError}>
      <p>Connection failed: {result.message}</p>
      {hint && (
        <div className="mt-2 border-t border-red-200 pt-2 dark:border-red-900">
          <p className="font-medium">Firewall check</p>
          <p>
            Allow {hint.protocol.toUpperCase()} port {hint.port} from the Vantage server to{" "}
            <span className="font-mono">{hint.destination}</span>.
          </p>
          {hint.reason && <p className="text-xs">{hint.reason}</p>}
        </div>
      )}
    </div>
  );
}

interface Props {
  integration: IntegrationInfo;
  /** The saved instance, when editing. */
  instance?: Instance;
}

/** Add or edit form for one instance, generated from the integration's config schema. */
export function InstanceForm({ integration, instance }: Props) {
  const editing = instance !== undefined;
  const api = useApiClient();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fields = useMemo(() => configFields(integration.schema), [integration.schema]);
  const secretsSet = useMemo(() => instance?.secrets_set ?? {}, [instance]);

  const [name, setName] = useState(instance?.name ?? "");
  const [site, setSite] = useState(instance?.site ?? "");
  const [tags, setTags] = useState(instance?.tags.join(", ") ?? "");
  const [enabled, setEnabled] = useState(instance?.enabled ?? true);
  const [values, setValues] = useState(() => initialValues(fields, instance?.config));
  const [clearSecrets, setClearSecrets] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testNote, setTestNote] = useState<string | null>(null);

  function onChange(key: string, value: FieldValue) {
    setValues((v) => ({ ...v, [key]: value }));
    setTestResult(null);
    setTestNote(null);
  }

  function onClearSecret(key: string, clear: boolean) {
    setClearSecrets((s) => {
      const next = new Set(s);
      if (clear) next.add(key);
      else next.delete(key);
      return next;
    });
    if (clear) setValues((v) => ({ ...v, [key]: "" }));
  }

  /** Check the form; returns the request parts, or null after showing what's wrong. */
  function collect() {
    const built = buildConfig(fields, values, { editing, secretsSet, clearSecrets });
    const metaErrors: Record<string, string> = {};
    const trimmed = name.trim();
    if (!trimmed) metaErrors.name = "Required.";
    else if (trimmed.length > NAME_MAX) metaErrors.name = `At most ${NAME_MAX} characters.`;
    if (site.trim().length > SITE_MAX) metaErrors.site = `At most ${SITE_MAX} characters.`;
    const tagList = parseTags(tags);
    if (tagList.length > TAGS_MAX) metaErrors.tags = `At most ${TAGS_MAX} tags.`;
    else if (tagList.some((t) => t.length > TAG_MAX)) {
      metaErrors.tags = `Each tag at most ${TAG_MAX} characters.`;
    }
    const all = { ...built.errors, ...metaErrors };
    setErrors(all);
    if (Object.keys(all).length) {
      setFormError("Check the highlighted fields.");
      return null;
    }
    setFormError(null);
    return {
      name: trimmed,
      site: site.trim() || null,
      tags: tagList,
      enabled,
      config: built.config,
    };
  }

  const test = useMutation({
    mutationFn: (parts: { config: Record<string, unknown>; name: string; saved: boolean }) =>
      parts.saved && instance
        ? api.testInstance(instance.id)
        : api.testUnsavedInstance({
            type_id: integration.type_id,
            name: parts.name,
            config: parts.config,
          }),
    onSuccess: (result) => setTestResult(result),
    onError: (error) => setFormError(errorText(error)),
  });

  const save = useMutation({
    mutationFn: (parts: NonNullable<ReturnType<typeof collect>>) =>
      instance
        ? api.updateInstance(instance.id, parts)
        : api.createInstance({ type_id: integration.type_id, ...parts }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["instances"] });
      await navigate("/devices");
    },
    onError: (error) => setFormError(errorText(error)),
  });

  function onTest() {
    setTestResult(null);
    setTestNote(null);
    const parts = collect();
    if (!parts) return;
    if (instance && !configChanged(fields, parts.config, instance.config)) {
      test.mutate({ config: parts.config, name: parts.name, saved: true });
      return;
    }
    // Changed settings are tested unsaved, which needs every stored secret re-entered:
    // the browser never has the stored ones.
    // TODO(maintainer): drop this once POST /instances/test can fill omitted secrets from a
    // saved instance (see the TODO in backend/app/api/instances.py).
    const missing = fields.filter((f) => f.secret && secretsSet[f.key] && !(f.key in parts.config));
    if (instance && missing.length) {
      setTestNote(
        `To test changed settings before saving, enter ${missing
          .map((f) => f.label)
          .join(", ")} again, or save first and test from the device list.`,
      );
      return;
    }
    test.mutate({ config: parts.config, name: parts.name, saved: false });
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (save.isPending) return;
    const parts = collect();
    if (parts) save.mutate(parts);
  }

  const metaInput = (key: string) => ({
    "aria-invalid": errors[key] ? true : undefined,
    "aria-describedby": errors[key] ? `meta-${key}-error` : undefined,
  });

  return (
    <form onSubmit={onSubmit} noValidate className="max-w-xl space-y-4">
      {formError && (
        <p role="alert" className={alertError}>
          {formError}
        </p>
      )}
      <div>
        <label htmlFor="meta-name" className={label}>
          Name
        </label>
        <input
          id="meta-name"
          className={input}
          autoComplete="off"
          maxLength={NAME_MAX}
          value={name}
          aria-required
          {...metaInput("name")}
          onChange={(e) => setName(e.target.value)}
        />
        {errors.name && (
          <p id="meta-name-error" className={fieldError}>
            {errors.name}
          </p>
        )}
      </div>
      <div>
        <label htmlFor="meta-site" className={label}>
          Site <span className="font-normal text-slate-500 dark:text-slate-400">(optional)</span>
        </label>
        <input
          id="meta-site"
          className={input}
          autoComplete="off"
          maxLength={SITE_MAX}
          value={site}
          {...metaInput("site")}
          onChange={(e) => setSite(e.target.value)}
        />
        {errors.site && (
          <p id="meta-site-error" className={fieldError}>
            {errors.site}
          </p>
        )}
      </div>
      <div>
        <label htmlFor="meta-tags" className={label}>
          Tags <span className="font-normal text-slate-500 dark:text-slate-400">(optional)</span>
        </label>
        <input
          id="meta-tags"
          className={input}
          autoComplete="off"
          value={tags}
          {...metaInput("tags")}
          aria-describedby={errors.tags ? "meta-tags-error" : "meta-tags-hint"}
          onChange={(e) => setTags(e.target.value)}
        />
        <p id="meta-tags-hint" className={fieldHint}>
          Separate tags with commas.
        </p>
        {errors.tags && (
          <p id="meta-tags-error" className={fieldError}>
            {errors.tags}
          </p>
        )}
      </div>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          className="h-4 w-4"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        Enabled
      </label>

      <fieldset className="space-y-4 border-t border-slate-200 pt-4 dark:border-slate-800">
        <legend className="pr-2 text-sm font-semibold">{integration.name} settings</legend>
        <ConfigFields
          fields={fields}
          values={values}
          errors={errors}
          onChange={onChange}
          editing={editing}
          secretsSet={secretsSet}
          clearSecrets={clearSecrets}
          onClearSecret={onClearSecret}
        />
      </fieldset>

      {testNote && (
        <p role="status" className={alertInfo}>
          {testNote}
        </p>
      )}
      {testResult && <TestOutcome result={testResult} />}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={save.isPending} className={buttonPrimary}>
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Add device"}
        </button>
        <button
          type="button"
          onClick={onTest}
          disabled={test.isPending}
          className={`${buttonSecondary} h-10`}
        >
          {test.isPending ? "Testing…" : "Test connection"}
        </button>
        <button
          type="button"
          onClick={() => void navigate("/devices")}
          className={`${buttonSecondary} h-10`}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
