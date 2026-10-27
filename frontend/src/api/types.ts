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

export interface AuditEntry {
  id: number;
  user_id: number | null;
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
