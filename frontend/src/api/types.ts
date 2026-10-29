// Request and response shapes of the backend API (Contract 3, /api/v1).
// Keep in sync with backend/app/api/*.py.

export type Role = "admin" | "viewer";

export interface SetupStatus {
  setup_required: boolean;
}

/** Body of `POST /setup` and `POST /auth/login`. */
export interface Credentials {
  username: string;
  password: string;
}

/** `GET /me`, and the body of a successful login or setup. */
export interface Me {
  id: number;
  username: string;
  role: Role;
  /** Sent back in the X-CSRF-Token header. Held in memory only, never in storage. */
  csrf_token: string;
}

/** `GET /users`, and the response of creating or updating one. Never carries a password or hash. */
export interface UserInfo {
  id: number;
  username: string;
  role: Role;
  /** ISO 8601 timestamps. */
  created_at: string;
  last_login_at: string | null;
}

/** Body of `POST /users`. */
export interface UserCreate {
  username: string;
  password: string;
  role: Role;
}

/** Body of `POST /me/password`. */
export interface PasswordChange {
  current_password: string;
  new_password: string;
}

/** Who made an audited change: the API (a user), the admin CLI, or the backend itself. */
export type ActorType = "user" | "cli" | "system";

export interface AuditEntry {
  id: number;
  user_id: number | null;
  actor_type: ActorType;
  action: string;
  target_type: string;
  target_id: string | null;
  /** ISO 8601 timestamp. */
  at: string;
  /** Already redacted by the backend: secret fields hold "<changed>". */
  changes: Record<string, unknown>;
}

export interface AuditQuery {
  /** 1 to 200; the backend defaults to 50. */
  limit?: number;
  /** Keyset paging: pass the smallest id already seen. */
  before_id?: number;
}

export type Category =
  | "firewall"
  | "hypervisor"
  | "storage"
  | "switch"
  | "tunnel"
  | "cloud"
  | "media"
  | "host"
  | "generic";

export interface IntegrationInfo {
  type_id: string;
  name: string;
  category: Category;
  /** JSON schema of the config; secret fields are flagged `writeOnly`. */
  schema: Record<string, unknown>;
}

/** The poll collector's view of an instance (in memory: it resets on a backend restart). */
export interface InstanceStatus {
  /** `unknown`: the collector doesn't poll this instance (disabled, or scrape-only). */
  state: "pending" | "up" | "down" | "unknown";
  checked_at: string | null;
  last_success_at: string | null;
  /** Short and secret-free. */
  error: string | null;
}

/** `GET /instances` and `GET /instances/{id}`. Never carries a secret value. */
export interface Instance {
  id: string;
  type_id: string;
  name: string;
  site: string | null;
  tags: string[];
  enabled: boolean;
  /** Non-secret config fields only. */
  config: Record<string, unknown>;
  /** For each secret field: whether a value is stored. */
  secrets_set: Record<string, boolean>;
  status: InstanceStatus;
  created_at: string;
  updated_at: string;
}

/** Body of `POST /instances`. `config` holds every field, secrets included. */
export interface InstanceCreate {
  type_id: string;
  name: string;
  site?: string | null;
  tags?: string[];
  enabled?: boolean;
  config: Record<string, unknown>;
}

/**
 * Body of `PATCH /instances/{id}`. `config` is merged into the stored one: an omitted
 * field (a secret left blank) keeps its value; `null` clears an optional field.
 */
export interface InstanceUpdate {
  name?: string;
  site?: string | null;
  tags?: string[];
  enabled?: boolean;
  config?: Record<string, unknown>;
}

/** Body of `POST /instances/test`: an unsaved config. */
export interface InstanceTest {
  type_id: string;
  name?: string;
  config: Record<string, unknown>;
}

export interface FirewallHint {
  protocol: "tcp" | "udp" | "icmp";
  /** null for icmp (ICMP echo has no port). */
  port: number | null;
  destination: string;
  reason: string;
}

/** `POST /instances/test` and `POST /instances/{id}/test`. Never contains a secret. */
export interface TestResult {
  ok: boolean;
  message: string;
  hint: FirewallHint | null;
}

/** One card type an instance offers (`GET /instances/{id}/cards`). */
export interface CardSpec {
  /** Matches a card component registered in `cards/registry.ts`. */
  card_type: string;
  title: string;
  /** Grid width and height, in cells of a 12-column grid. */
  default_size: [number, number];
}

/** One card on a dashboard: which instance and card type, where it sits, and its settings. */
export interface CardPlacement {
  instance_id: string;
  card_type: string;
  x: number;
  y: number;
  w: number;
  h: number;
  settings: Record<string, string | number | boolean | null>;
}

export interface DashboardLayout {
  cards: CardPlacement[];
}

/** `GET/PUT /dashboards/{id}`. */
export interface Dashboard {
  id: number;
  name: string;
  layout: DashboardLayout;
}
