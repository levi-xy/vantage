// Turns an integration's config JSON schema (GET /integrations) into form fields, and
// form values back into the `config` object the instance API expects.
//
// Secret fields (property-level `writeOnly`, guaranteed by the backend) are write-only:
// they never get an initial value, and a blank one is left out of the request, which the
// backend reads as "unchanged" on edit and "use the default / required" on create.

type Json = Record<string, unknown>;

export type FieldKind =
  "string" | "integer" | "number" | "boolean" | "enum" | "list" | "unsupported";

export interface ConfigField {
  key: string;
  kind: FieldKind;
  label: string;
  description?: string;
  /** `required` in the schema: the backend refuses a config without it. */
  required: boolean;
  /** The schema allows `null` (an optional value that can be cleared). */
  nullable: boolean;
  secret: boolean;
  /** Schema default (never present for secrets). */
  default?: unknown;
  options?: (string | number)[];
  /** Element kind of a `list` field. */
  itemKind?: "string" | "integer" | "number";
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  // No `pattern`: schemas carry Python regex syntax, which a browser may read differently.
  // The backend checks it.
  placeholder?: string;
}

/** What a form input holds: text for strings, numbers and lists; a flag for checkboxes. */
export type FieldValue = string | boolean;
export type FormValues = Record<string, FieldValue>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Follow a local `#/$defs/...` reference; anything else is left as is. */
function resolve(node: Json, root: Json): Json {
  const ref = node.$ref;
  if (typeof ref !== "string" || !ref.startsWith("#/$defs/")) return node;
  const defs = root.$defs;
  const target = isObject(defs) ? defs[ref.slice("#/$defs/".length)] : undefined;
  return isObject(target) ? { ...target, ...node, $ref: undefined } : node;
}

function scalarKind(node: Json): FieldKind {
  if (Array.isArray(node.enum)) return "enum";
  if ("const" in node) return "enum";
  switch (node.type) {
    case "string":
      return "string";
    case "integer":
      return "integer";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "array":
      return "list";
    default:
      return "unsupported";
  }
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

/** "api_token" → "Api token", for schemas without a title. */
function humanize(key: string): string {
  const text = key.replace(/[_-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The fields of a config schema, in schema order. */
export function configFields(schema: Json): ConfigField[] {
  const properties = schema.properties;
  if (!isObject(properties)) return [];
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);

  return Object.entries(properties).map(([key, raw]): ConfigField => {
    const prop = isObject(raw) ? resolve(raw, schema) : {};
    let node = prop;
    let nullable = false;
    const branches = Array.isArray(prop.anyOf) ? prop.anyOf.filter(isObject) : null;
    if (branches) {
      const nonNull = branches.filter((b) => b.type !== "null");
      nullable = nonNull.length < branches.length;
      node = nonNull.length === 1 && nonNull[0] ? resolve(nonNull[0], schema) : {};
    }
    let kind = scalarKind(node);
    let itemKind: ConfigField["itemKind"];
    if (kind === "list") {
      const items = isObject(node.items) ? resolve(node.items, schema) : {};
      const k = scalarKind(items);
      if (k === "string" || k === "integer" || k === "number") itemKind = k;
      else kind = "unsupported";
    }
    let options: (string | number)[] | undefined;
    if (kind === "enum") {
      const values = Array.isArray(node.enum) ? node.enum : [node.const];
      options = values.filter(
        (v): v is string | number => typeof v === "string" || typeof v === "number",
      );
    }
    const secret = prop.writeOnly === true;
    const examples = Array.isArray(prop.examples) ? prop.examples : [];
    const example = examples.find((e) => typeof e === "string" || typeof e === "number");
    return {
      key,
      kind,
      label: str(prop.title) ?? humanize(key),
      description: str(prop.description),
      required: required.has(key),
      nullable,
      secret,
      // The backend never serves a secret's default; ignore one anyway.
      default: secret ? undefined : prop.default,
      options,
      itemKind,
      minimum: num(node.minimum) ?? num(node.exclusiveMinimum),
      maximum: num(node.maximum) ?? num(node.exclusiveMaximum),
      minLength: num(node.minLength),
      maxLength: num(node.maxLength),
      placeholder: secret || example === undefined ? undefined : String(example),
    };
  });
}

function toInput(field: ConfigField, value: unknown): FieldValue {
  if (field.kind === "boolean") return value === true;
  if (value === null || value === undefined) return "";
  if (field.kind === "list") return Array.isArray(value) ? value.map(String).join("\n") : "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return "";
}

/**
 * Initial form values: the stored config when editing, the schema defaults when adding.
 * Secret fields always start empty: their values never reach the browser.
 */
export function initialValues(fields: ConfigField[], stored?: Record<string, unknown>): FormValues {
  const values: FormValues = {};
  for (const field of fields) {
    if (field.secret) {
      values[field.key] = "";
    } else if (stored) {
      values[field.key] = toInput(field, stored[field.key]);
    } else {
      values[field.key] = toInput(field, field.default);
    }
  }
  return values;
}

export interface BuildOptions {
  /** Editing a saved instance: a blank secret keeps the stored value. */
  editing: boolean;
  /** Secret fields whose stored value is set (from `secrets_set`). */
  secretsSet?: Record<string, boolean>;
  /** Optional secret fields the user asked to clear (sent as null). */
  clearSecrets?: ReadonlySet<string>;
}

export interface BuildResult {
  config: Record<string, unknown>;
  /** Field key → message, for the fields that need fixing before sending. */
  errors: Record<string, string>;
}

function parseNumber(field: ConfigField, text: string): number | string {
  const value = Number(text);
  if (!Number.isFinite(value)) return "Enter a number.";
  if (field.kind === "integer" && !Number.isInteger(value)) return "Enter a whole number.";
  return value;
}

function checkRange(field: ConfigField, value: number): string | null {
  if (field.minimum !== undefined && value < field.minimum) return `At least ${field.minimum}.`;
  if (field.maximum !== undefined && value > field.maximum) return `At most ${field.maximum}.`;
  return null;
}

function checkText(field: ConfigField, text: string): string | null {
  if (field.minLength !== undefined && text.length < field.minLength) {
    return `At least ${field.minLength} characters.`;
  }
  if (field.maxLength !== undefined && text.length > field.maxLength) {
    return `At most ${field.maxLength} characters.`;
  }
  return null;
}

/**
 * Form values → the request's `config`. Only basic checks run here (required, number
 * format, ranges, lengths); the backend validates everything against the real schema.
 */
export function buildConfig(
  fields: ConfigField[],
  values: FormValues,
  options: BuildOptions,
): BuildResult {
  const config: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    const raw = values[field.key];
    if (field.kind === "unsupported") continue; // the backend's default applies
    if (field.kind === "boolean") {
      config[field.key] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw : "";

    if (field.secret) {
      if (options.clearSecrets?.has(field.key) && field.nullable) {
        config[field.key] = null;
      } else if (text !== "") {
        const problem = checkText(field, text);
        if (problem) errors[field.key] = problem;
        config[field.key] = text;
      } else if (field.required && !(options.editing && options.secretsSet?.[field.key] === true)) {
        errors[field.key] = "Required.";
      }
      // Blank: left out. Unchanged on edit; the schema default (if any) on create.
      continue;
    }

    if (text.trim() === "") {
      if (field.required) errors[field.key] = "Required.";
      else if (field.nullable) config[field.key] = null;
      else if (field.default !== undefined) config[field.key] = field.default;
      continue;
    }

    switch (field.kind) {
      case "integer":
      case "number": {
        const value = parseNumber(field, text.trim());
        if (typeof value === "string") errors[field.key] = value;
        else {
          const problem = checkRange(field, value);
          if (problem) errors[field.key] = problem;
          config[field.key] = value;
        }
        break;
      }
      case "enum": {
        const match = field.options?.find((o) => String(o) === text);
        if (match === undefined) errors[field.key] = "Pick one of the options.";
        else config[field.key] = match;
        break;
      }
      case "list": {
        const items = text
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
        if (field.itemKind === "string") {
          config[field.key] = items;
        } else {
          const numbers = items.map(Number);
          if (numbers.some((n) => !Number.isFinite(n))) errors[field.key] = "One number per line.";
          else config[field.key] = numbers;
        }
        break;
      }
      default: {
        const problem = checkText(field, text);
        if (problem) errors[field.key] = problem;
        config[field.key] = text;
      }
    }
  }
  return { config, errors };
}

/** Whether `config` changes any non-secret value of `stored`, or sets/clears a secret. */
export function configChanged(
  fields: ConfigField[],
  config: Record<string, unknown>,
  stored: Record<string, unknown>,
): boolean {
  return fields.some((field) => {
    if (!(field.key in config)) return false;
    if (field.secret) return true;
    return JSON.stringify(config[field.key] ?? null) !== JSON.stringify(stored[field.key] ?? null);
  });
}
