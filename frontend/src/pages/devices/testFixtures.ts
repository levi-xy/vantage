// Test-only data for the device pages. Example addresses and obviously fake values only.

import type { Instance, IntegrationInfo } from "../../api";

/** As served by GET /integrations for the backend's test integration (pydantic output). */
export const FAKE_SCHEMA = {
  properties: {
    host: { examples: ["192.0.2.10"], title: "Host", type: "string" },
    port: { default: 443, title: "Port", type: "integer", minimum: 1, maximum: 65535 },
    verify_tls: { default: true, title: "Verify Tls", type: "boolean" },
    api_token: { format: "password", title: "Api Token", type: "string", writeOnly: true },
    snmp_community: {
      anyOf: [{ format: "password", type: "string", writeOnly: true }, { type: "null" }],
      title: "Snmp Community",
      writeOnly: true,
    },
    client_secret: { format: "password", title: "Client Secret", type: "string", writeOnly: true },
  },
  required: ["host", "api_token"],
  title: "FakeConfig",
  type: "object",
};

export const FAKE_TYPE: IntegrationInfo = {
  type_id: "fake_device",
  name: "Fake device",
  category: "generic",
  schema: FAKE_SCHEMA,
};

export const INSTANCE_ID = "00000000-0000-4000-8000-000000000001";

export function exampleInstance(overrides: Partial<Instance> = {}): Instance {
  return {
    id: INSTANCE_ID,
    type_id: "fake_device",
    name: "nas-1",
    site: "lab",
    tags: ["storage"],
    enabled: true,
    config: { host: "10.0.0.10", port: 8443, verify_tls: true },
    secrets_set: { api_token: true, client_secret: true, snmp_community: false },
    status: { state: "up", checked_at: null, last_success_at: null, error: null },
    created_at: "2026-01-02T03:04:05Z",
    updated_at: "2026-01-02T03:04:05Z",
    ...overrides,
  };
}
