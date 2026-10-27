import { buildConfig, configChanged, configFields, initialValues } from "./schemaForm";
import { FAKE_SCHEMA } from "./testFixtures";

describe("configFields", () => {
  it("reads kinds, labels, defaults and secrets", () => {
    const fields = configFields(FAKE_SCHEMA);
    expect(fields.map((f) => [f.key, f.kind, f.required, f.secret, f.nullable])).toEqual([
      ["host", "string", true, false, false],
      ["port", "integer", false, false, false],
      ["verify_tls", "boolean", false, false, false],
      ["api_token", "string", true, true, false],
      ["snmp_community", "string", false, true, true],
      ["client_secret", "string", false, true, false],
    ]);
    expect(fields[0]).toMatchObject({ label: "Host", placeholder: "192.0.2.10" });
    expect(fields[1]).toMatchObject({ default: 443, minimum: 1, maximum: 65535 });
  });

  it("resolves enums through $defs and optional anyOf branches", () => {
    const fields = configFields({
      $defs: { Version: { enum: ["v1", "v2c", "v3"], type: "string", title: "Version" } },
      properties: {
        version: { $ref: "#/$defs/Version", default: "v2c" },
        mode: { anyOf: [{ enum: ["a", "b"], type: "string" }, { type: "null" }], default: null },
        timeout: { anyOf: [{ type: "number" }, { type: "null" }] },
        ports: { type: "array", items: { type: "integer" } },
        nested: { type: "object", properties: {} },
        dns_name: { type: "string" },
      },
    });
    expect(fields.map((f) => [f.key, f.kind])).toEqual([
      ["version", "enum"],
      ["mode", "enum"],
      ["timeout", "number"],
      ["ports", "list"],
      ["nested", "unsupported"],
      ["dns_name", "string"],
    ]);
    expect(fields[0]).toMatchObject({ options: ["v1", "v2c", "v3"], default: "v2c" });
    expect(fields[1]).toMatchObject({ options: ["a", "b"], nullable: true });
    expect(fields[3]).toMatchObject({ itemKind: "integer" });
    expect(fields[5]?.label).toBe("Dns name");
  });

  it("never keeps a default or example for a secret", () => {
    const [field] = configFields({
      properties: {
        key: { type: "string", writeOnly: true, default: "example-token", examples: ["x"] },
      },
    });
    expect(field?.default).toBeUndefined();
    expect(field?.placeholder).toBeUndefined();
    expect(initialValues(configFields(FAKE_SCHEMA), {})).toMatchObject({ api_token: "" });
  });

  it("handles a schema without properties", () => {
    expect(configFields({})).toEqual([]);
  });
});

describe("initialValues", () => {
  const fields = configFields(FAKE_SCHEMA);

  it("starts from the defaults when adding", () => {
    expect(initialValues(fields)).toEqual({
      host: "",
      port: "443",
      verify_tls: true,
      api_token: "",
      snmp_community: "",
      client_secret: "",
    });
  });

  it("starts from the stored config when editing, with every secret blank", () => {
    const stored = { host: "10.0.0.10", port: 8443, verify_tls: false, api_token: "leak" };
    expect(initialValues(fields, stored)).toEqual({
      host: "10.0.0.10",
      port: "8443",
      verify_tls: false,
      api_token: "",
      snmp_community: "",
      client_secret: "",
    });
  });

  it("uses the schema default for a field the stored config lacks", () => {
    expect(initialValues(fields, { host: "10.0.0.10" })).toMatchObject({
      port: "443",
      verify_tls: true,
    });
  });
});

describe("buildConfig", () => {
  const fields = configFields(FAKE_SCHEMA);
  const filled = {
    host: " 10.0.0.10 ",
    port: "8443",
    verify_tls: true,
    api_token: "example-token",
    snmp_community: "",
    client_secret: "",
  };

  it("converts values and leaves blank secrets out on create", () => {
    expect(buildConfig(fields, filled, { editing: false })).toEqual({
      config: { host: " 10.0.0.10 ", port: 8443, verify_tls: true, api_token: "example-token" },
      errors: {},
    });
  });

  it("flags required fields, bad numbers and ranges", () => {
    const { errors } = buildConfig(
      fields,
      { ...filled, host: "  ", port: "8.5", api_token: "" },
      { editing: false },
    );
    expect(errors).toEqual({
      host: "Required.",
      port: "Enter a whole number.",
      api_token: "Required.",
    });
    expect(buildConfig(fields, { ...filled, port: "70000" }, { editing: false }).errors).toEqual({
      port: "At most 65535.",
    });
    expect(buildConfig(fields, { ...filled, port: "abc" }, { editing: false }).errors).toEqual({
      port: "Enter a number.",
    });
  });

  it("falls back to the default when an optional field is emptied", () => {
    expect(buildConfig(fields, { ...filled, port: "" }, { editing: false }).config.port).toBe(443);
  });

  it("on edit: a blank secret is unchanged (omitted) even when required", () => {
    const { config, errors } = buildConfig(
      fields,
      { ...filled, api_token: "" },
      { editing: true, secretsSet: { api_token: true } },
    );
    expect(errors).toEqual({});
    expect(config).not.toHaveProperty("api_token");
    expect(config).not.toHaveProperty("snmp_community");
  });

  it("on edit: a required secret that isn't stored still has to be entered", () => {
    const { errors } = buildConfig(
      fields,
      { ...filled, api_token: "" },
      { editing: true, secretsSet: { api_token: false } },
    );
    expect(errors).toEqual({ api_token: "Required." });
  });

  it("clears an optional secret only when asked", () => {
    const opts = {
      editing: true,
      secretsSet: { api_token: true, snmp_community: true },
      clearSecrets: new Set(["snmp_community", "api_token"]),
    };
    const { config } = buildConfig(fields, { ...filled, api_token: "" }, opts);
    expect(config.snmp_community).toBeNull();
    // api_token isn't nullable: "clear" is ignored, blank stays unchanged.
    expect(config).not.toHaveProperty("api_token");
  });

  it("builds enums, lists and nullable fields", () => {
    const listFields = configFields({
      properties: {
        mode: { enum: ["a", 2], title: "Mode" },
        ports: { type: "array", items: { type: "integer" } },
        names: { type: "array", items: { type: "string" } },
        note: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
        nested: { type: "object" },
      },
      required: ["mode"],
    });
    expect(
      buildConfig(
        listFields,
        { mode: "2", ports: "80\n 443 \n\n", names: "a\nb", note: "", nested: "" },
        { editing: false },
      ),
    ).toEqual({ config: { mode: 2, ports: [80, 443], names: ["a", "b"], note: null }, errors: {} });
    expect(
      buildConfig(listFields, { mode: "x", ports: "80\nhttp", names: "" }, { editing: false })
        .errors,
    ).toEqual({ mode: "Pick one of the options.", ports: "One number per line." });
  });
});

describe("configChanged", () => {
  const fields = configFields(FAKE_SCHEMA);
  const stored = { host: "10.0.0.10", port: 8443, verify_tls: true };

  it("is false when only blank secrets differ", () => {
    expect(configChanged(fields, { ...stored }, stored)).toBe(false);
  });

  it("is true for a changed value or an entered secret", () => {
    expect(configChanged(fields, { ...stored, port: 9443 }, stored)).toBe(true);
    expect(configChanged(fields, { ...stored, api_token: "example-token" }, stored)).toBe(true);
  });

  it("treats a field missing from the stored config as its default", () => {
    // Stored before verify_tls (default true) existed: the round trip through the form
    // must not count as a change, or testing an untouched device asks for secrets.
    const old = { host: "10.0.0.10", port: 8443 };
    const { config } = buildConfig(fields, initialValues(fields, old), {
      editing: true,
      secretsSet: { api_token: true },
    });
    expect(config.verify_tls).toBe(true);
    expect(configChanged(fields, config, old)).toBe(false);
    expect(configChanged(fields, { ...config, verify_tls: false }, old)).toBe(true);
  });
});
