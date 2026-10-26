# Security

This file collects Vantage's security rules. The hard rules are in `AGENTS.md`.

## Audit redaction rules

Every create, update and delete writes an `audit_log` row through
`backend/app/core/audit.py`. Secret values never reach that table. Before an entry is
stored, every secret field in `changes` is replaced with the literal `"<changed>"`.

A field is treated as secret, at any nesting depth, when any of these hold:

- **Name heuristic.** The key is normalised to lowercase letters and digits only, so
  `API-Key`, `apiKey` and `api_key` all become `apikey`. The key is secret if the
  normalised form contains any of: `secret`, `token`, `password`, `passwd`,
  `passphrase`, `community`, `key`, `psk`, `authorization`, `credential`. This is broad
  on purpose. A harmless name like `keyboard_layout` is redacted too, because
  over-redaction is the safe direction.
- **Schema.** The key is a `SecretStr` or `SecretBytes` field of the Pydantic schema the
  caller passes, such as an integration's `config_schema`. Optional fields, aliases and
  nested models are included.
- **Explicit list.** The caller lists the key in `secret_keys`.
- **Value type.** The value is a `SecretStr` or `SecretBytes`, or raw bytes such as an
  encrypted blob, whatever its key.

Further rules:

- **Present secret keys.** A secret key that is present is always `"<changed>"`, even
  when its value is `None` or empty. For updates, the old and new values collapse into a
  single `"<changed>"`.
- **Omitted keys.** An omitted key is not recorded at all. On PATCH, an omitted secret
  means "unchanged", so callers pass only the fields that were actually sent.
- **Keys.** Keys must be strings. Bytes keys are decoded first. Any other key type is
  refused, because turning it into a string could get it past the name checks.
- **Values.** Values of unknown types are refused rather than stored as text, because an
  object's text form could contain a secret.
- **Fail closed.** Changes to `instance` and `channel` targets must come with the schema
  that marks their secret fields. If no schema is given, `record()` raises.
- **Logins.** Successful logins and logouts are audited, with no secret data. Failed
  logins that reached the password check are audited as `login_failed`. The username is
  stored only when it matches an existing user (`target_id` is then that user's id).
  Otherwise it is recorded as `"<unknown>"`, because the typed text is
  attacker-controlled and is sometimes a password. The submitted password is never
  passed in. Attempts refused by the rate limiter (429) are not recorded, so the table
  can't be flooded.
- **Tests.** `backend/tests/test_audit.py` sends known secret values through every
  audited flow. It then scans the raw `audit_log` rows and the `GET /api/v1/audit`
  response to prove none of the values appear.
