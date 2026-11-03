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

/** `no_data`: still open, but no fresh value to tell whether the condition holds. */
export type AlertState = "firing" | "no_data" | "resolved";

/** Why an open event has no data. Later versions may add reasons. */
export type AlertNoDataReason =
  | "source_stale"
  | "source_down"
  | "metric_missing"
  | "query_failed"
  | "prometheus_unreachable"
  | "metrics_not_collected"
  | (string & {});

/** Vantage's own alerts (no rule). Later versions may add kinds. */
export type SystemAlertKind =
  | "prometheus_unreachable"
  | "metrics_not_collected"
  | "evaluator_stalled"
  | "key_mismatch"
  | (string & {});

export type AlertEndReason =
  "recovered" | "rule_disabled" | "rule_deleted" | "rule_changed" | "closed_by_user";

/** `GET /alerts/events`, newest first. One event per (rule, series) episode. */
export interface AlertEvent {
  id: number;
  /** null once the rule is deleted. */
  rule_id: number | null;
  /** The device, when the series named an existing instance. */
  instance_id: string | null;
  /** Snapshots taken when the event opened. */
  rule_name: string;
  severity: string;
  /** Filtered by the backend: sensitive values are "<redacted>". */
  labels: Record<string, string>;
  fingerprint: string;
  state: AlertState;
  /** Set while `state` is `no_data`. */
  no_data_reason?: AlertNoDataReason | null;
  started_at: string;
  /** When the event entered its current state (null for old events). */
  state_since?: string | null;
  ended_at: string | null;
  end_reason: AlertEndReason | null;
  summary: string;
  /** The messages queued about this event, one per channel and change. */
  deliveries: AlertDelivery[];
  /** `system`: one of Vantage's own alerts; it has no rule and closes by itself. */
  source?: "rule" | "system";
  system_kind?: SystemAlertKind | null;
}

export type DeliveryKind = "firing" | "resolved" | "rule_change" | "summary" | "no_data";

export type DeliveryStatus =
  "pending" | "sending" | "delivered" | "failed" | "folded" | "cancelled";

/** One message about an event to one channel. Never the text or a channel setting. */
export interface AlertDelivery {
  /** null once the channel is deleted. */
  channel_id: number | null;
  channel_name: string | null;
  kind: DeliveryKind;
  status: DeliveryStatus;
  attempts: number;
  /** A fixed code such as "timeout" or "unauthorized". */
  error_kind: string | null;
  created_at: string;
  finished_at: string | null;
}

/** `GET /alerts/delivery-status`: messages waiting 15+ minutes or failed in the last 24 h. */
export interface DeliveryStatusSummary {
  stuck: number;
  /** Firing system alerts, oldest first. */
  system_alerts?: { system_kind: SystemAlertKind; since: string }[];
}

export interface AlertEventQuery {
  /** 1 to 200; the backend defaults to 50. */
  limit?: number;
  before_id?: number;
  state?: AlertState;
}

export type AlertSeverity = "info" | "warning" | "critical";

/** `GET /alerts/rules` (admin only). */
export interface AlertRule {
  id: number;
  name: string;
  expr: string;
  for_seconds: number;
  severity: AlertSeverity;
  /** Limits the rule to one device; null means all. */
  instance_id: string | null;
  channel_ids: number[];
  enabled: boolean;
  /** How the rule's alerts resolve: the value seen failing the condition, or an empty
   * result (when the expression can't be split into a value and a condition). */
  recovery_mode?: "value" | "empty_result";
  /** The last query error the backend saw for the rule; short, no secrets. */
  error?: string | null;
}

/** Body of `POST /alerts/rules`; `PATCH` takes any subset. */
export type AlertRuleInput = Omit<AlertRule, "id" | "recovery_mode" | "error">;

/** `GET /alerts/templates` (admin only): a suggested rule. */
export interface AlertTemplate {
  id: string;
  name: string;
  description: string;
  integration_types: string[];
  expr: string;
  for_seconds: number;
  severity: AlertSeverity;
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
  /** A device colour name (`app/deviceColors.ts`); null only for an uncoloured row. */
  color: string | null;
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
  /** Omitted: the backend assigns the first unused colour. */
  color?: string;
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
  color?: string;
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

/**
 * One card on a dashboard: which instance and card type, where it sits, and its settings.
 * `instance_id` is null for a dashboard-wide card ("Devices healthy" and the like).
 */
export interface CardPlacement {
  instance_id: string | null;
  card_type: string;
  x: number;
  y: number;
  w: number;
  h: number;
  settings: Record<string, string | number | boolean | null>;
}

export interface DashboardLayout {
  /** 1 (or absent): saved before dashboard-wide cards were cards. See `cards/layout.ts`. */
  version?: number;
  cards: CardPlacement[];
}

/** `GET/PUT /dashboards/{id}`. */
export interface Dashboard {
  id: number;
  name: string;
  layout: DashboardLayout;
}

/** `GET /dashboards`: every dashboard, in tab order. The first one is the main one. */
export interface DashboardSummary {
  id: number;
  name: string;
  position: number;
}

/** `POST /dashboards`: an empty layout, or a copy of `copy_from`'s. */
export interface DashboardCreate {
  name: string;
  copy_from?: number;
}

/** `PATCH /dashboards/{id}`: rename or move. */
export interface DashboardUpdate {
  name?: string;
  position?: number;
}

/** `GET/POST /links`: a connection between two instances, drawn on the network map. */
export interface DeviceLink {
  id: number;
  a_instance_id: string;
  b_instance_id: string;
  label: string | null;
  a_port: string | null;
  b_port: string | null;
}

export type DeviceLinkCreate = Omit<DeviceLink, "id">;

/** Pushes on `WS /api/v1/ws`. Server to client only; never carries config or secrets. */
export type LiveMessage =
  | { type: "instance_status"; instance_id: string; status: InstanceStatus }
  /** Sent after each successful poll: refetch what you show for this instance. */
  | { type: "refresh"; instance_id: string }
  /** An alert fired or resolved: ids, times and a short summary only. */
  | {
      type: "alert_event";
      event_id: number;
      rule_id: number | null;
      instance_id: string | null;
      fingerprint: string;
      severity: string;
      state: AlertState;
      started_at: string;
      ended_at: string | null;
      summary: string;
    }
  /** Every ~2 s while connected: summed traffic of the throughput card's saved ports. */
  | { type: "live_rate"; in_bps: number; out_bps: number; ports: number; at: string }
  | { type: "pong" };

/** Body of `POST /query`. Times are Unix seconds, `step` seconds. No start/end = instant query. */
export interface QueryRequest {
  query: string;
  start?: number;
  end?: number;
  step?: number;
}

/** One series. `values` are `[unix_seconds, "value"]` pairs; values are strings (`"NaN"`, `"+Inf"`). */
export interface QuerySeries {
  labels: Record<string, string>;
  values: [number, string][];
}

/** Response of `POST /query` (backend/app/api/query.py). */
export interface QueryResult {
  result_type: "vector" | "matrix" | "scalar" | "string";
  series: QuerySeries[];
  warnings: string[];
}

/** An alert channel (`GET /channels`, admin only). The bot token is write-only. */
export interface Channel {
  id: number;
  type: "telegram";
  name: string;
  /** Non-secret settings only: `{chat_id}`. */
  config: { chat_id?: string };
  /** Whether each secret is stored (`{bot_token: true}`). Never the value. */
  secrets_set: Record<string, boolean>;
  /** Receives Vantage's own alerts (Prometheus unreachable, ...). */
  system_alerts?: boolean;
}

/** Body of `POST /channels`. */
export interface ChannelCreate {
  type: "telegram";
  name: string;
  config: { chat_id: string; bot_token: string };
  system_alerts?: boolean;
}

/** Body of `PATCH /channels/{id}`. An omitted `bot_token` keeps the stored one. */
export interface ChannelUpdate {
  name?: string;
  config?: { chat_id?: string; bot_token?: string };
  system_alerts?: boolean;
}

/** Result of `POST /channels/{id}/test`. */
export interface ChannelTestResult {
  ok: boolean;
  message: string;
}
