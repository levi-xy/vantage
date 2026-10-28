# Writing an integration

This is the guide for adding a device type to Vantage. Read [`AGENTS.md`](../AGENTS.md) first: it owns the contracts (the Integration interface, the HTTP API, the SSRF guard) and the hard rules. This file does not repeat them; it says how to build to them, using the code that is on `main` as the example. Where this guide and the code disagree, the code wins: open an issue or fix the guide.

The three reference integrations are the templates. Read the one that matches your style before you write anything:

| Style | Reference | Source |
|---|---|---|
| Scrape-only: Prometheus scrapes an exporter | `linux_host` (node_exporter) | `backend/app/integrations/builtin/linux_host/integration.py` |
| Polled over HTTP: Vantage makes the request | `http_check` | `backend/app/integrations/builtin/http_check/integration.py` |
| Polled over ICMP: Vantage pings | `ping_check` | `backend/app/integrations/builtin/ping_check/integration.py` |

Shared helpers: `backend/app/integrations/builtin/common.py`. Their tests, which are also templates: `backend/tests/integrations/builtin/`.

Contents

1. [Anatomy of an integration](#1-anatomy-of-an-integration)
2. [The Integration interface](#2-the-integration-interface)
3. [The config schema](#3-the-config-schema)
4. [Secrets](#4-secrets)
5. [Host and URL fields](#5-host-and-url-fields)
6. [Three styles of integration](#6-three-styles-of-integration)
7. [Talking to the device](#7-talking-to-the-device)
8. [`poll()`, failures and status](#8-poll-failures-and-status)
9. [Metrics](#9-metrics)
10. [Scrape targets](#10-scrape-targets)
11. [Connection test and firewall hints](#11-connection-test-and-firewall-hints)
12. [Cards](#12-cards)
13. [Registering the integration](#13-registering-the-integration)
14. [Deployment assumptions](#14-deployment-assumptions)
15. [Tests](#15-tests)
16. [Checklist before you open the PR](#16-checklist-before-you-open-the-pr)
17. [Documenting your integration](#17-documenting-your-integration)
18. [Index of integration docs](#18-built-in-integrations)

---

## 1. Anatomy of an integration

One package per integration under `backend/app/integrations/builtin/`, named after its `type_id`:

```
backend/app/integrations/builtin/http_check/
├── __init__.py        # re-exports the class and its config model
└── integration.py     # the config model and the Integration subclass
```

`__init__.py` is three lines (see `http_check/__init__.py`). Everything else is in `integration.py`: a pydantic model for the config, and a subclass of `Integration` (`backend/app/integrations/base.py`) that sets the class variables and implements the methods in section 2. Helpers used by more than one integration live in `builtin/common.py`; don't copy them into your package, and add to `common.py` only what a second integration would also need.

The class variables, as `http_check` sets them:

```python
class HttpCheck(Integration):
    type_id = TYPE_ID                      # "http_check"
    display_name = "HTTP check"
    category: ClassVar[Category] = "generic"
    version = "0.1.0"
    config_schema = HttpCheckConfig
```

- `type_id`: stable forever, because it is stored in every instance row and appears in metric names. It must match `[a-z][a-z0-9_]{0,31}` (`registry.TYPE_ID_PATTERN`) and be unique.
- `category`: one of the `Category` values in `base.py`.
- `cloud_hosts`: leave the default `()` for anything on a private network. Declare a fixed public API host (for example `("api.cloudflare.com",)`) only for a cloud service; the guard then allows exactly those hosts, over HTTPS only, and only if they resolve to public addresses (AGENTS.md Contract 5).
- `config_schema`: the pydantic model from section 3. Annotate `category` as `ClassVar[Category]`, as the references do.

---

## 2. The Integration interface

Contract 1 in AGENTS.md is the specification; `base.py` is the code. What each member is for:

| Member | Required | What it is for |
|---|---|---|
| `type_id`, `display_name`, `category`, `version`, `config_schema`, `cloud_hosts` | yes (`cloud_hosts` defaults to `()`) | Identity and the config model. Checked by the conformance suite (section 15). |
| `async test_connection(ctx) -> TestResult` | yes (abstract) | One live check, run when an admin presses "Test" on a saved or unsaved config. Returns `ok`, a human message that never contains a secret, and a `FirewallHint` when a firewall is a likely cause of failure. |
| `scrape_targets(ctx) -> list[ScrapeTarget]` | no (default `[]`) | Where Prometheus should scrape an exporter for this instance. Synchronous, no network. Served by `/sd/targets` (section 10). |
| `async poll(ctx) -> list[Sample]` | no (default `[]`) | Vantage itself reads the device on an interval and returns metric samples, which the collector serves on `/metrics` (sections 8 and 9). |
| `poll_interval_seconds() -> int` | no (default 60) | How often `poll()` runs. Must be a positive int. The scheduler clamps it to 10 s to 86 400 s. |
| `cards() -> list[CardSpec]` | yes (abstract) | The dashboard cards this integration provides (section 12). |
| `firewall_hints(ctx) -> list[FirewallHint]` | yes (abstract) | Which traffic the Vantage backend (or Prometheus) needs allowed to reach the device (section 11). |

`InstanceContext` (`base.py`) is the only thing the methods get. It carries:

- `ctx.config`, and `ctx.config_as(MyConfig)` for typed access. The config is your model, validated, with secrets decrypted into `SecretStr` fields. It is never in `repr`.
- `ctx.instance_id`, `ctx.name`, `ctx.labels` (the four standard labels while polling; empty for connection tests).
- `ctx.http_client(...)`, `ctx.snmp_client(...)`, `ctx.ping_client(...)`: the only way to reach the network (section 7).

An integration never builds an `InstanceContext`; the instance API and the poll collector do.

`Sample` is `name`, `labels`, `value`, `type` (`"gauge"` or `"counter"`). `ScrapeTarget` is `address` (`host:port`), `metrics_path` (default `/metrics`), `scheme` (`http` or `https`).

---

## 3. The config schema

`config_schema` is a pydantic v2 `BaseModel`. It does three jobs:

1. Its JSON schema (`GET /api/v1/integrations`, built by `integrations/schema.py`) is what the "Add device" form is generated from. Whatever you put in `Field(title=..., description=..., default=..., examples=..., ge=..., le=..., max_length=...)` and the type annotations (`Literal["http", "https"]` becomes a choice) ends up in that schema, so write a `title` and a `description` for every field the admin has to understand.
2. It validates what the admin enters, on create, update and test (a failure is a 422), and again every time the stored config is loaded for a poll or for service discovery.
3. It decides where each field is stored (Contract 2): `SecretStr` fields are encrypted into `instances.secrets`; every other field goes into `instances.config`, which is **plaintext in the database and returned by `GET /instances`**. Treat every non-secret field as public to all signed-in users.

Rules that follow from the code:

- **Validate strictly and never echo the input.** A validator's message is shown to the admin and may be logged. `http_check` raises `ValueError("not a valid URL")`, not the URL.
- **Validation runs on every load.** A config that is valid today and invalid after you tighten the schema in an upgrade stays in the database. When the poll collector loads it, it fails validation and the instance goes `down` with the fixed message `stored config is not valid for this integration` (the validation error is deliberately not shown, since it would echo the input). Service discovery skips such an instance and logs only its id. So tightening a schema is an upgrade note, not a free change. `ping_check` has a test for this path (`test_collector_degrades_config_made_invalid_by_upgrade`).
- **Bound everything.** Use `ge`/`le`/`max_length` so an admin (or a bad stored row) cannot make the integration do unbounded work. `ping_check` also uses a `model_validator` so that `count` times the reply timeout fits inside the ping client's 15 s limit.
- **Defaults must be safe.** `verify_tls` defaults to `True`; a switch that weakens security is opt-in, per instance, and its description says when to use it.
- **Use the guard's parser for hosts.** `checked_host()` in `common.py` wraps `net_guard.parse_host`, so a host accepted at validation is one the guard can check later. Call it from a `field_validator` as `linux_host` and `ping_check` do.

---

## 4. Secrets

Rules, in the order they are enforced:

1. **A secret field is a top-level field typed `SecretStr` or `SecretStr | None`.** Nothing else. Nested models, lists, `dict`s and `SecretBytes` are rejected by the conformance suite, and `instance_config.secret_field_keys` raises rather than store a nested secret in plaintext.
2. **Anything credential-shaped must be a secret, and its name should say so with an underscore.** The conformance suite fails a plain-typed field when its name matches `(^|_)(password|passwd|passphrase|secret|token|community|api_?key|private_key|auth_key|priv_key)$` (`SECRET_NAME_PATTERN` in `tests/integrations/conformance.py`): the credential word must be the whole name or follow an underscore, at the end. So `api_token`, `bearer_token`, `admin_password`, `snmp_community`, `token` and `api_key` are caught; `bearertoken`, `authtoken`, `mytoken` and `token_value` are **not**, and nothing catches a credential under a name such as `auth` or `pass`. The check is a safety net, not a definition: name every secret field `*_token`, `*_password`, `*_secret`, `*_community` or `*_key` with the underscore, and type it `SecretStr`, whatever the check would say. SNMP communities and v3 keys are secrets too (section 7).
3. **Secrets are write-only.** The served schema flags them `writeOnly`, and `schema.py` strips every `default`, `examples`, `example`, `const` and `enum` from them, so no secret-derived value is served. Instance responses never contain them; they carry only `secrets_set: {field: bool}`, which the form uses to show "set" or "not set". On PATCH an omitted secret means "unchanged". An empty or whitespace-only secret is refused; an optional secret that is `None` is simply not stored.
4. **Do not put an example secret value in a secret field's `description` or `json_schema_extra`.** `schema.py` removes the value-carrying keywords above, but it serves author text such as `description` and `json_schema_extra` as written. A description is for telling the admin what the field is: `description="Optional. Sent as an Authorization header to this URL only."` (`http_check`).
5. **Secrets never leave the backend**: not in an API response, a log line, an audit entry, a `TestResult.message`, a `FirewallHint`, an exception message you raise, or a metric label. The audit log replaces secret fields with `"<changed>"`. As defence in depth the instance API scrubs the decrypted secret values out of a `TestResult`, and the collector scrubs them out of error text and drops any sample that carries one in a label value; do not rely on that, it is a last line.
6. **Use a secret only where it must go.** `http_check` puts its bearer token in an `Authorization` header built inside `poll()`/`test_connection()` and requests the configured URL only, with redirects not followed, so the token cannot be sent to a host a redirect names.

A secret you may need many times is read with `cfg.bearer_token.get_secret_value()` at the point of use, not stored in another variable that outlives the call.

---

## 5. Host and URL fields

### How the save-time check finds hosts

Before an instance is created, updated or tested, `instance_config.check_hosts` resolves every host in the config and runs the SSRF guard on the answers, so a denied address is refused when the admin types it, with a clear message. It finds hosts **by field name only**:

| Field name | Treated as |
|---|---|
| `host`, or any name ending `_host` | a host (IP literal or DNS name), checked as configured |
| `url`, or any name ending `_url` | a URL: its host part is checked |

Details that matter (all in `instance_config.configured_hosts`):

- Only non-secret config fields are looked at, and only string values that are not blank.
- The port used to resolve a `host` field is the config field named `port` if it is an integer in 1 to 65535, otherwise 443. A URL uses its own port, or 443 / 80 for `https` / `http`. The port is used only to resolve; the guard checks the address, not the port.
- For a URL field the scheme must be `http` or `https`, there must be a host, and **there must be no username or password** (`user:pw@host`): the instance is refused, because the config is stored in plaintext and returned by the API.
- For a URL, the host checked is the **ASCII (punycode) form**: `url.raw_host.decode("ascii")`, the same form the guarded transport resolves and connects to. A URL with an internationalised host name is therefore checked as `xn--...`. A `host` field has no such conversion: `parse_host` rejects non-ASCII names, so a Unicode host must be entered in punycode. An integration that parses its own URL field (as `http_check` does in its validator) must use `raw_host` too, or the two parsers could disagree about which host a URL names.
- The check fails closed: a name that does not resolve at save time is refused.

A field with any other name (`address`, `server`, `endpoint`, `base`) is **not** checked at save time. The connection is still checked by the guard when it is made, so this is not a bypass, but the admin gets a worse error and a saved-but-unreachable instance. So: **name host fields `host`/`*_host` and URL fields `url`/`*_url`.** If your device genuinely needs another name, ask the maintainer (see the open question below) instead of working around it.

### Open decision: marking host fields explicitly

This is not implemented. Today host fields are found by name only, as above. There is a `TODO(maintainer)` at the top of `backend/app/integrations/instance_config.py` proposing `Field(json_schema_extra={"vantage_host": True})` with the naming convention kept as a fallback. Do not use `vantage_host` in an integration; it has no effect. The maintainer decides; this section is updated when they do.

### URL fields hold no credentials

- No `user:pw@` in a URL: refused at save time (above) and by your own validator (`http_check` does the same).
- **Anything in a URL is plaintext config.** That includes the query string: `https://host/status?token=abc` stores `abc` in the config column and returns it from `GET /instances` to every user. Credentials go in a `SecretStr` field and are attached to the request in code (a header, or a parameter built at request time).
- Say so in the field's `description`, as `http_check` does. Query strings stay allowed, because health endpoints use them; the warning is the safeguard.
- Never log or echo a URL. `common.connection_failure` names only `host:port`.

---

## 6. Three styles of integration

Pick by who makes the request. The scheduler decides whether to poll an instance by one test, `collectors.scheduler.polls(cls)`: **the class overrides `poll`**. A scrape-only integration must therefore not define `poll` at all (not even one returning `[]`), or it becomes polled.

| | Scrape-only | Polled | Polled over ICMP |
|---|---|---|---|
| Example | `linux_host` | `http_check` | `ping_check` |
| Who talks to the device | Prometheus, directly | The backend, every interval | The backend, every interval |
| You implement | `scrape_targets`, `test_connection` | `poll`, `test_connection` | `poll`, `test_connection` |
| Metrics | Exporter's native names (`node_cpu_seconds_total`) | `vantage_<type_id>_*` on Vantage's `/metrics` | `vantage_<type_id>_*` on Vantage's `/metrics` |
| Standard labels added by | `/sd/targets` | the poll collector | the poll collector |
| Instance status | `unknown` | `pending`, `up`, `down` | `pending`, `up`, `down` |
| `vantage_instance_up` | not emitted (use Prometheus' `up`) | emitted | emitted |
| Client | `ctx.http_client()` in `test_connection` only | `ctx.http_client()` | `ctx.ping_client()` |
| Firewall traffic | Prometheus to exporter (and Vantage for the test) | Backend to device | Backend to device, ICMP |

An integration may do both (scrape an exporter and poll an API); the rules for each half apply.

**Scrape-only constraints.** Prometheus' config is fixed (Contract 4) and nothing device-specific is ever added to it, so a scraped target cannot carry per-device credentials, headers or TLS settings. The exporter must answer unauthenticated, over `https` only with a certificate Prometheus trusts. If the device needs credentials, poll it instead.

---

## 7. Talking to the device

All outbound traffic goes through `net_guard` (Hard rule 5, Contract 5). The context gives you three guarded clients:

```python
ctx.http_client(*, verify=True, base_url="", headers=None) -> httpx.AsyncClient
ctx.snmp_client(host, auth, *, port=161) -> GuardedSnmpClient    # auth: SnmpV2c | SnmpV3
ctx.ping_client(host, *, count=3, reply_timeout=1.0) -> GuardedPingClient
```

- **Never** import `httpx.AsyncClient`, `socket`, an SNMP library, or an ICMP library into integration code, and never open a raw socket. `ping_check`'s test parses every file under `builtin/` and fails on imports of `socket`, `asyncio_dgram`, `icmplib` or `scapy`, and on the text `SOCK_RAW`. It does **not** look for a hand-built `httpx.AsyncClient` (or `requests`, `aiohttp`, ...); using one is still forbidden, and reviewers check for it by hand.
- The guarded clients resolve the host once, check every address, and connect to the checked address; redirects are not followed automatically. HTTP requests have a 5 s connect and 15 s total limit by default.
- **Read-only.** Send `GET` (or `HEAD`/`OPTIONS` where a device needs it). Never `POST`/`PUT`/`PATCH`/`DELETE` to a device endpoint that changes state, even if the library or API offers it (Hard rule 2). SNMP is `get`/`walk` only. ICMP echo changes nothing. If an API can only be read through a `POST` (some GraphQL and query APIs), **do not build it first**: stop and raise it as a `TODO(maintainer)` question (and in the PR), saying which endpoint and why the request cannot change state. Hard rule 2 is the maintainer's to interpret, not the author's.
- `ctx.http_client(verify=False)` is for an instance whose admin has opted into a self-signed certificate. Expose it as a per-instance config field defaulting to `True`, as `http_check` does.
- The guard's own exceptions are `NetGuardError` (policy: denied address, unresolvable name, cloud host over HTTP, ...), and for ping `PingError`. Their messages are safe to display (host as configured, an address, or fixed text).
- Wrap a whole request in `asyncio.timeout(...)` as `http_check` does, so DNS, connect, TLS and headers share one budget; the guarded client's 15 s cap sits underneath it.
- Stream when you only need headers or a bounded body: `client.stream("GET", url)`. `linux_host` caps what it reads at 8 MiB with `_read_limited`.

### SNMP

`ctx.snmp_client(host, auth, port=161)` returns a `GuardedSnmpClient` (`net_guard.py`). `auth` is `SnmpV2c(community=SecretStr)` or `SnmpV3(username, auth_key, priv_key, auth_protocol, priv_protocol)`; both are importable from `app.core.net_guard`.

- **Read-only by construction.** The client exposes `get(oids)` and `walk(oid, max_rows=10_000)` and nothing else: no `set`. `test_snmp_is_read_only` in `tests/test_net_guard.py` fails if a write operation appears. `walk` stays inside the subtree you ask for.
- **OIDs are numeric** (`"1.3.6.1.2.1.2.2.1.10"`; a leading dot is accepted). Symbolic names raise `ValueError`; there is no MIB lookup. Both calls return `SnmpVarBind(oid, value)` where `value` is an `int` (counters, gauges, ticks), a `str` (OIDs, IP addresses), `bytes` (octet strings) or `None` (noSuchObject, noSuchInstance, endOfMibView). Octet strings are device-supplied: decode defensively and bound what you put in a label.
- **Secrets map to top-level `SecretStr` fields** of your config, passed straight in:

  ```python
  class MySnmpConfig(BaseModel):
      host: str                       # found by name by the save-time check
      community: SecretStr            # v2c
      # v3 instead: username: str (plaintext config), auth_key / priv_key: SecretStr | None

  client = ctx.snmp_client(cfg.host, SnmpV2c(community=cfg.community))
  ```

  An integration that supports several versions uses a version field and `SecretStr | None` for each secret, and builds `SnmpV2c` or `SnmpV3` from it. SNMPv3 privacy needs an auth key (the client raises `ValueError` otherwise): validate that combination in a `model_validator` so the admin sees it at save time, without echoing values. The v3 `username` is a plain `str` in `SnmpV3`, so it is stored in plaintext config.
- **Limits.** 5 s per attempt, 2 retries, 15 s in total; `ctx.snmp_client()` passes no overrides. A timeout raises `SnmpError`; so does any other SNMP failure, with text that never contains the community or keys. `SnmpError` is not a `NetGuardError`, so in `poll()` it shows as `poll failed (SnmpError)`. In `test_connection()` catch it and return a `TestResult` yourself, with a UDP firewall hint.
- **Private addresses only.** SNMP never gets the cloud exemption.
- **Close the client** (`client.close()`, in `try/finally`) when you are done.
- **Firewall:** `FirewallHint(protocol="udp", port=port, destination=host, reason=...)`.
- **State which OIDs you read**, and why each is read-only, in the integration's doc file and PR.

**Testing SNMP.** `netfake.py` has no SNMP fake (SNMP is UDP through pysnmp, not the HTTP socket layer). Do what `tests/test_net_guard.py` (`SnmpRecorder`) and `tests/integrations/test_base.py` do: monkeypatch `net_guard.get_cmd` and `net_guard.walk_cmd` with async stand-ins that record their arguments and return rows built from pysnmp types (`ObjectName`, `OctetString`, `Counter32`, `Integer32`, `IpAddress`, `NoSuchInstance`), so the real guard and client code still run. Assert that the recorded target is the checked address (use `fake_guards` and `netfake.DNS`), that a denied host is refused with no call recorded, that only the OIDs you intend are requested, and that the community or keys never appear in a message, label, log or response. `SnmpRecorder` is defined inside `test_net_guard.py` and cannot be imported: copy the few lines you need.

**`poll()` and `test_connection()` must `await` all their I/O.** The scheduler cuts a poll off with `asyncio.timeout`, which can only interrupt code that is waiting at an `await`. A blocking call (`requests`, `time.sleep`, a synchronous SNMP or file read, a CPU-heavy loop) cannot be interrupted, and because everything runs on one event loop it stalls the polling of every instance and the API with it. Use `await asyncio.sleep`, not `time.sleep`, and the async clients above.

---

## 8. `poll()`, failures and status

### What the collector does

`collectors/scheduler.py` runs one asyncio task per enabled instance whose class overrides `poll`:

1. Every 30 s a supervisor re-reads the instance list and starts, restarts (after an edit) or stops tasks.
2. Each poll loads the stored config, decrypts the secrets and validates `{**config, **secrets}` against your `config_schema`, then builds a context with the standard labels.
3. It calls `poll(ctx)` under `asyncio.timeout(min(interval, 60))`: at most 60 s, and never longer than the interval. `poll_interval_seconds()` is clamped to 10 s to 86 400 s.
4. It checks the result is a `list[Sample]`, validates and stores the samples (section 9) and marks the instance `up`; or records a failure and marks it `down`.

A poll that raises, hangs or returns garbage affects only its own instance. Old samples of a failed instance are dropped, not served stale.

### Choosing the poll interval

`poll_interval_seconds()` defaults to 60. Return a fixed positive `int`. The scheduler reads it once when an instance's poll task starts and clamps it to 10 s to 86 400 s (a value that is not a number becomes 10 s). Pick the slowest interval that still serves the dashboard and alerts: every poll is traffic to a real device and time on the shared event loop. Cheap local checks (ping, HTTP check) suit 30 to 60 s; a heavy walk or a rate-limited cloud API should use minutes and stay well inside the provider's limits. The poll is cut off after `min(interval, 60)` s, so a poll that legitimately needs more than 60 s has to be split up; a longer interval does not give it more time.

### Raise on failure, return on a result

- **No usable answer** (timeout, refused, TLS failure, guard refusal, the host is silent): **raise**. The instance goes `down` and `vantage_instance_up` is 0. `http_check` lets the exception from `_check` propagate; `ping_check` raises its own `NoReply` when no echo was answered.
- **A real answer that says "bad"**: **return samples**. A wrong HTTP status is a result (`vantage_http_check_success 0`, plus the status code); partial packet loss is a result. This is what lets an alert rule say what the device answered.
- Do not catch broad `Exception` in `poll()` to return an empty list: that reports `up` with no data.

### What the status shows

Instance responses carry `status: {state, checked_at, last_success_at, error}` (Contract 3):

| State | Meaning |
|---|---|
| `pending` | Enabled and polled by the collector, not polled yet (new, just enabled, or just after a restart). |
| `up` | The last poll returned a valid list of samples. |
| `down` | The last poll failed. `error` says why, briefly. |
| `unknown` | The collector does not poll this instance: disabled, or a scrape-only integration. |

The status is in memory: it resets on restart.

`error` is built so it cannot leak. The scheduler does not store an exception's own text, because it can carry URLs, headers or response bodies; it stores the exception type, a fixed text, or (for `NetGuardError` only) the guard's message, which holds just the host as configured and a fixed reason. Today:

| What happened | `error` |
|---|---|
| `TimeoutError` or `httpx.TimeoutException` (including the 60 s limit) | `poll timed out` |
| `NetGuardError` | `refused by the network guard: <the guard's message>` |
| any other exception | `poll failed (<ExceptionType>)`, for example `poll failed (NoReply)` |
| `poll()` returned something other than a list of `Sample` | `poll returned something other than a list of samples` |
| stored config no longer validates | `stored config is not valid for this integration` |
| stored secrets can't be decrypted | `stored secrets can't be decrypted (was the master key changed?)` |
| building the network guard failed | `network guard unavailable (<ExceptionType>)` |
| storing the returned samples failed | `storing poll results failed (<ExceptionType>)` |

So a failed poll shows the exception type, one of the fixed texts above, or the guard's message. If the admin needs to tell failures apart, **give your exception a distinct class name** (`NoReply`, not `Exception`); that name is all they will see. Decrypted secret values are also scrubbed from the text, and a guard message must stay free of config values (it names the host as configured and a fixed reason).

`test_connection()` is different: it returns a `TestResult` with a message you write, so it can be specific (`HTTP 503, expected 2xx or 3xx`). It must still never contain exception text or a secret. If it raises instead, the instance API reports only the exception type (`connection test failed (<Type>)`), or `connection test timed out` after 30 s; a `NetGuardError` is shown as `connection refused by the network guard: ...`.

---

## 9. Metrics

Contract 1 is the rule; the checks below are what `collectors/store.py` enforces when it stores your samples. A sample that fails any check is dropped (counted in `vantage_poll_dropped_samples`), never fatal.

- **Name**: `vantage_<type_id>_<metric>` and valid Prometheus syntax (`[a-zA-Z_][a-zA-Z0-9_]*`). Put the unit in the name (`_seconds`, `_bytes`, `_ratio`). Define them as constants, as the references do (`METRIC_SUCCESS = f"vantage_{TYPE_ID}_success"`). Names that belong to Vantage itself (`vantage_instance_up`, `vantage_poll_*`) are refused.
- **Type**: `gauge` or `counter`. A counter is exposed with a `_total` suffix and must be finite and non-negative. Don't define a gauge `x_total` next to a counter `x`: the later family is dropped to keep the exposition valid.
- **Labels**: add your own (`{"disk": "sda"}`) but never the four standard ones, any label starting with `__`, or an invalid label name. At most 32 labels per sample, each value at most 1024 characters. **Never put a secret, URL or free-form device text in a label value.**
- **Limits**: at most 10 000 samples per instance per poll; a duplicate (same name and labels) is dropped. Keep label cardinality bounded.
- **Standard labels**: the collector adds `vantage_instance_id`, `vantage_instance`, `vantage_integration` and `vantage_site` to every sample, and `/sd/targets` adds the same four to scrape targets. An instance with no site gets `vantage_site=""`; Prometheus treats an empty label as absent, so those series carry three labels. This is intended: never substitute a placeholder such as `"none"`; the UI shows "No site".
- **Scraped exporters** keep their native metric names (`node_cpu_seconds_total`); only polled metrics use the `vantage_<type_id>_` prefix.

The polled metrics of `http_check` are a template:

```python
return [
    Sample(name=METRIC_SUCCESS, value=1.0 if outcome.success else 0.0, type="gauge"),
    Sample(name=METRIC_STATUS, value=float(outcome.status), type="gauge"),
    Sample(name=METRIC_DURATION, value=outcome.seconds, type="gauge"),
]
```

The collector also emits, per polled instance, `vantage_instance_up`, `vantage_poll_duration_seconds`, `vantage_poll_last_success_timestamp_seconds` and `vantage_poll_dropped_samples`; you do not.

---

## 10. Scrape targets

`scrape_targets(ctx)` returns the exporter addresses Prometheus should scrape:

```python
def scrape_targets(self, ctx: InstanceContext) -> list[ScrapeTarget]:
    cfg = ctx.config_as(LinuxHostConfig)
    return [ScrapeTarget(address=cfg.address, metrics_path=METRICS_PATH, scheme=cfg.scheme)]
```

- It is synchronous and does no network I/O. `/sd/targets` calls it for every enabled instance on every request.
- `address` is a plain `host:port` (bracket IPv6: `[fd00::20]:9100`; `common.host_port()` does that). It must be ASCII, printable, with no space, `/`, `\`, `@`, `?`, `#` or `%`, a port, and no userinfo. `metrics_path` starts with `/`, has no space, `?` or `#`, and is at most 2048 characters. A malformed target is left out and logged by instance id.
- `/sd/targets` returns Prometheus HTTP SD groups with the four standard labels, `__metrics_path__` and `__scheme__`. It is bearer-protected with the Prometheus token.
- **Targets are checked, then left out when refused.** Prometheus connects to targets itself, outside `net_guard`, so for every target `/sd/targets` runs the instance's guard first (Contract 5): the host must resolve (5 s limit) to allowed addresses, and a declared cloud host must use `https`. A target that fails DNS or the guard is **omitted from the response**, so it disappears from Prometheus rather than showing up with `up == 0`. If a freshly added host does not appear in Prometheus, check that it resolves from the backend and is not in a denied range before looking at the exporter.
- Prometheus resolves hostnames again when it scrapes, so only IP-literal targets are fully protected against DNS rebinding. Prefer telling admins to enter an IP.
- An instance whose type is unknown, whose secrets can't be decrypted, whose stored config no longer validates, or whose `scrape_targets` raises is skipped; the others are unaffected.

---

## 11. Connection test and firewall hints

### `test_connection(ctx) -> TestResult`

Called by `POST /instances/{id}/test` (saved instance) and `POST /instances/test` (unsaved config), by an admin. It is the one place a scrape-only integration talks to the device itself. It should:

- make **one** read-only attempt that proves what the instance needs: for `http_check`, the same request `poll()` makes; for `linux_host`, that the page really is node_exporter; for `ping_check`, the same ping as `poll()`;
- return `ok=True` with a short message that is useful to see (`node_exporter 1.8.1 on lab-host-1`);
- return `ok=False` for every failure, with the reason in plain words;
- never include a secret, a URL or an exception's text in the message. Device-supplied text (a version, a node name) is untrusted: `linux_host` passes it through `_quote()` (printable ASCII, 64 characters).

Turn request failures into results with `common.connection_failure(exc, where="host:port", hint=hint)`:

| Failure | Message | Hint attached |
|---|---|---|
| `NetGuardError` | `refused by the network guard: ...` | no (a firewall rule won't help) |
| timeout | `timed out waiting for host:port` | yes |
| `httpx.ConnectError` from TLS | `TLS handshake with host:port failed (is the certificate trusted?)` | no |
| other `httpx.ConnectError` | `could not connect to host:port (refused or unreachable)` | yes |
| other `httpx.HTTPError` | `request to host:port failed (<Type>)` | no |
| anything else | re-raised | the instance API shows only the type |

A response that arrived but is wrong (wrong status, not the expected software) is a failure result you write yourself, with no firewall hint. `ping_check` returns the ICMP hint when not one reply came back, and treats partial loss as `ok`.

### `firewall_hints(ctx) -> list[FirewallHint]`

The hints tell the admin which traffic to allow. `FirewallHint` is `protocol`, `port`, `destination` (the instance host as configured) and `reason`; the model enforces that `tcp`/`udp` have a port and `icmp` has none:

```python
FirewallHint(protocol="tcp", port=port, destination=host,
             reason="The Vantage backend requests the checked URL on this port.")   # http_check

FirewallHint(protocol="icmp", port=None, destination=host,
             reason="The Vantage backend pings this host: allow ICMP echo requests "
                    "to it and its echo replies back.")                               # ping_check
```

Say in `reason` **who** connects: the Vantage backend (polled, and every connection test) or Prometheus (scraped). Return one hint per distinct flow. The API attaches only the **first** hint to a timed-out test, so put the most likely one first.

---

## 12. Cards

`cards()` returns the dashboard cards for the integration:

```python
def cards(self) -> list[CardSpec]:
    return [CardSpec(card_type="http-check", title="HTTP check", default_size=(2, 2))]
```

- `card_type` is a lowercase slug (`[a-z][a-z0-9_-]*`) that names a frontend card folder; `title` is non-empty; `default_size` is `(width, height)` in grid units, both at least 1. The conformance suite checks these.
- Return a list even for one card. The list is served by `GET /instances/{id}/cards`.
- The card components themselves are frontend work (M5.5). A backend integration task defines the specs, and says in its doc section which metrics each card is expected to read.

---

## 13. Registering the integration

An integration exists only when it is listed in `backend/app/integrations/registry.py`. Nothing is ever imported from a name in user input or configuration.

```python
from app.integrations.builtin.ping_check import PingCheck

BUILTIN_INTEGRATIONS: tuple[type[Integration], ...] = (
    HttpCheck,  # http_check
    LinuxHost,  # linux_host
    PingCheck,  # ping_check
)
```

Add your import and one line, keeping the tuple sorted by `type_id`. Listing it is also what subjects it to the conformance suite: `tests/integrations/conftest.py` builds its subject list from `BUILTIN_INTEGRATIONS`, so you do not register tests anywhere. `Registry` refuses a duplicate or malformed `type_id` at startup.

---

## 14. Deployment assumptions

- **One backend worker.** The poll collector and `/metrics` live inside one process; with several uvicorn workers every worker would poll every instance and serve its own `/metrics`. This repo runs one worker: don't set `--workers` or `WEB_CONCURRENCY`.
- **ICMP.** `ctx.ping_client()` uses unprivileged ICMP datagram sockets, not raw sockets. `deploy/docker-compose.yml` sets the `net.ipv4.ping_group_range` sysctl on the backend container so the non-root user may open them; `NET_RAW` is not added. An integration that pings fails with a clear `PingError` when the permission is missing.
- **Network position.** Everything the integration reaches is reached from the backend container, and Prometheus reaches scrape targets from its own container. A firewall hint is about those two sources.

---

## 15. Tests

Test data follows AGENTS.md Hard rule 1: RFC 5737 (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and RFC 1918 addresses, `example.internal`-style names, and obviously fake values (`example-token-...`). Never a real host, IP, domain or credential; recorded fixtures must be scrubbed by hand. The fixture for `linux_host` (`tests/integrations/builtin/fixtures/node_exporter.prom`) is hand-written and trimmed.

### The conformance suite (free)

`tests/integrations/test_conformance.py` runs for every class in `BUILTIN_INTEGRATIONS`. It checks the class variables, that secrets are top-level `SecretStr`, that no credential-named field is a plain type, that cards and the poll interval are well-formed, and, through the real API, that the served schema flags secrets `writeOnly` and carries no secret default, example or mask. You write nothing for this; run it.

### The fake network (`tests/integrations/netfake.py`)

Both fixtures keep the **real** SSRF guard and the real clients and replace only the socket layer, so a test proves what your integration does with a response and that the request went through the guard to the checked address. Tests under `tests/integrations/builtin/` get `fake_net` and `fake_icmp` from that folder's `conftest.py`.

HTTP (`fake_net`, a `FakeNetwork`):

```python
fake_net.serve("10.0.0.20", 9100, http_response(200, body))   # routes are keyed by the CHECKED address
fake_net.serve("10.0.0.10", 80, handler)                      # or a function FakeRequest -> bytes
fake_net.fail("10.0.0.20", 9100, httpcore.ConnectTimeout())   # raised on connect
fake_net.requests    # every FakeRequest: method, target (path+query), headers (lowercased), address, port
fake_net.connects    # every (address, port) connected to
fake_net.tls_error = ssl.SSLError("...")                      # fail the handshake
fake_net.total_timeout = 0.2                                  # shorten the client's 15 s cap
# hang is a handler that never answers (to test your timeout)
```

ICMP (`fake_icmp`, a `FakeIcmp`):

```python
fake_icmp.answer("10.0.0.30")                       # always replies after 2 ms
fake_icmp.answer("10.0.0.30", drop(2))              # drop the echo with sequence 2
# an address that is not listed never answers; `never` is the responder for that
fake_icmp.sent       # every SentPing sent: address, type, code, sequence, payload
fake_icmp.opened     # sockets opened (empty if the guard refused before pinging)
fake_icmp.open_error / connect_error / send_error / reply_source / preload   # fault injection
```

Names resolve through `netfake.DNS` (`web.example.internal` to `10.0.0.10`, `node.example.internal` to `10.0.0.20`, `ping.example.internal` to `10.0.0.30`, `metadata.example.internal` to the metadata address `169.254.169.254`). Add an entry there for a new fake host rather than using a real name. Routes and responders are keyed by the resolved address, not by name.

Helpers: `make_ctx(MyIntegration, config_dict)` builds a context like a connection test's; `fake_guards` is a guard factory with the fake resolver, to pass to `make_client(db_url, net_guard_factory=fake_guards)` and to `PollScheduler(net_guard_factory=...)`; `http_response(status, body, ...)` builds the raw response bytes.

### Testing `cloud_hosts`

A cloud integration declares `cloud_hosts = ("api.example.com",)` (see `FakeCloudIntegration` in `tests/integrations/fake.py`). The guard lets only those exact names through, over HTTPS, and only when every DNS answer is public. `fake_guards(cls)` already passes `cls.cloud_hosts` to the guard, so `fake_net` works unchanged. You add two things: a DNS entry for the cloud name, and a stand-in for "public", because no RFC 5737 address counts as public. `tests/test_net_guard.py` has a `public_test_net` fixture for the second; copy it into your test module:

```python
@pytest.fixture
def public_test_net(monkeypatch: pytest.MonkeyPatch) -> None:
    test_net = IPv4Network("192.0.2.0/24")
    original = net_guard._is_public
    monkeypatch.setattr(net_guard, "_is_public", lambda addr: addr in test_net or original(addr))
    monkeypatch.setitem(DNS, "api.example.com", ["192.0.2.50"])   # DNS is netfake.DNS
```

Then `fake_net.serve("192.0.2.50", 443, http_response(200, body))` and call through `ctx.http_client()` with `https://api.example.com/...`. Tests to write (this setup was run and works): the https request succeeds and `fake_net.connects == [("192.0.2.50", 443)]`; the same host over `http://` raises `NetGuardError` with no connect; a cloud name whose DNS answer is private (`10.0.0.9`) is refused; a public name that is not in `cloud_hosts` is refused. Declare names, never addresses (the conformance suite rejects addresses), and keep per-instance hosts out of `cloud_hosts`.

### What your tests must cover

AGENTS.md (Testing rules, Review checklist) is the requirement. In practice, copy the structure of `test_http_check.py` / `test_linux_host.py` / `test_ping_check.py`:

1. **Schema**: defaults; accepted values; rejected values (bad URL, credentials in URL, out-of-range numbers, ambiguous numeric hosts); the validator's error never repeats the input; an IDN host is handled in its punycode form.
2. **Secrets**: no secret in `TestResult.message`, the hint, logs (`caplog`), the served schema, an API response or the metrics text; the secret reaches the device only where intended (the header, not a URL).
3. **`test_connection`**: success; wrong status or wrong software; refused connection, timeout (with the firewall hint), TLS failure (no hint), guard refusal (no hint); a hung device cut off by the timeout.
4. **Parsing** (`poll`): from a hand-written fixture, assert sample names, types, values and labels. Partial results. The failure paths raise.
5. **No writes**: assert on `fake_net.requests` that every method is `GET` (or whatever read method you use) and the paths are only the ones you intend (`assert_read_only` in the HTTP tests). For ICMP, `fake_icmp.sent` holds only echo requests to the checked address.
6. **Through the guard**: a host that resolves to a denied address (`metadata.example.internal`) is refused and `fake_net.connects == []` / `fake_icmp.opened == []`.
7. **Through the collector** (polled integrations): call `PollScheduler.poll_once(spec, integration)` against a `MetricsStore` and render `generate_latest(make_registry(store))`; assert the samples carry the four standard labels, that `vantage_site=""` appears for a site-less instance, that a failure marks the instance `down` with the expected `error`, and that other instances keep polling. See `collect()` in `test_ping_check.py`.
8. **Through the API and service discovery** (all): create the instance through `POST /api/v1/instances`, confirm a denied host gets a 422 on save, and for scraped integrations that `/sd/targets` returns the target with its four labels and leaves out one that resolves to a denied address (`test_api_to_service_discovery` in `test_linux_host.py`). The `admin` fixture in each test file is the pattern.
9. **Firewall hints and cards**: exact `FirewallHint` values and the `CardSpec` list.
10. **No raw clients**: `test_no_sockets_in_integration_code` in `test_ping_check.py` already covers every file under `builtin/`; keep your imports clean.

A new integration with a stored-config change after release also needs a test for the old stored config (see `test_collector_degrades_config_made_invalid_by_upgrade`).

Run everything with `python scripts/check.py` (set up first with `scripts/dev_setup.py`). Never weaken a check, test or security default to make it pass.

---

## 16. Checklist before you open the PR

Copy this into the PR description and tick it.

**Contract and structure**
- [ ] One package under `builtin/<type_id>/`; `type_id` is a stable lowercase slug, unique; `category`, `version`, `display_name` set
- [ ] Listed in `registry.py`, in `type_id` order
- [ ] `cloud_hosts` is `()` unless a fixed public API host is genuinely needed (and then HTTPS only)
- [ ] Style is right: a scrape-only class does not override `poll`; a polled class does

**Config and secrets**
- [ ] Every field has a `title` and, where needed, a `description`; numbers are bounded; safe defaults
- [ ] Every credential is a top-level `SecretStr` / `SecretStr | None`; no example secret in any description or `json_schema_extra`
- [ ] Host fields are named `host` / `*_host`, URL fields `url` / `*_url`; hosts validated with `checked_host`
- [ ] No credentials in a URL (validator refuses `user:pw@`), and the URL field's description warns that the URL is stored in plaintext
- [ ] Validator messages never echo the input
- [ ] If I tightened an existing schema: upgrade behaviour (instance goes `down`) is tested and noted

**Network**
- [ ] Only `ctx.http_client()` / `ctx.snmp_client()` / `ctx.ping_client()`; no `socket`, raw `httpx`, or other client
- [ ] Read-only: no state-changing request anywhere; tests assert the methods and paths
- [ ] Every `await`ed call in `poll()`/`test_connection()` is async; nothing blocking, no sleeps with `time.sleep`
- [ ] Requests have an explicit timeout inside the guarded client's 15 s cap
- [ ] If SNMP: the doc file lists which OIDs are read and why each is read-only; secrets are `SecretStr`; the client is closed
- [ ] If a read needs a `POST` (or anything not obviously a read): raised as a `TODO(maintainer)` before building
- [ ] If `cloud_hosts`: tested over https, over http (refused), with a private answer (refused) and with a non-declared public host (refused)
- [ ] `poll_interval_seconds()` chosen deliberately (section 8)
- [ ] Bodies read from a device are bounded

**Behaviour**
- [ ] `poll()` raises on "no answer" with a distinctly named exception, and returns samples for "answered, but bad"
- [ ] Metric names are `vantage_<type_id>_*` constants with units; labels bounded; no standard label, secret, URL or free text in a label
- [ ] `scrape_targets()` is synchronous and network-free; targets are plain `host:port`
- [ ] `test_connection()` returns a secret-free, URL-free, exception-text-free message; firewall hint on timeout and refused/unreachable
- [ ] `firewall_hints()` names who connects and the right protocol (`icmp` has `port=None`)
- [ ] `cards()` returns valid `CardSpec`s

**Tests and docs**
- [ ] Tests for schema, `test_connection` success and failure (with the hint), parsing, no writes, guard refusal, standard labels, API and `/sd/targets` where relevant, and every secret-leak assertion above
- [ ] Test data is RFC 5737 / RFC 1918 and obviously fake; fixtures contain nothing from a real network
- [ ] `docs/integrations/<type_id>.md` written from the template (section 17) and linked from the index in section 18; no `README`/`AGENTS.md` contract changed
- [ ] `python scripts/check.py` passes
- [ ] PR description: what changed, how it was tested, contract questions, new dependencies and why, `TODO(maintainer)` items

---

## 17. Documenting your integration

Each integration gets **its own file**, `docs/integrations/<type_id>.md`, so parallel PRs do not conflict. Add one row to the index in section 18 (in `type_id` order; if two PRs collide on the index, the later one rebases). Keep the file factual and short, and use this skeleton. A one-line "none" is better than a missing heading.

**Do not copy defaults, bounds or metric lists that the code already states**: they drift. Name the field and say what it is for, and link to the config model and the metric constants in the source. Document what the code cannot say: what is read and why it is read-only, the failure modes, the firewall flows, and caveats.

```markdown
# <Display name> (`<type_id>`)

**Style:** scrape-only | polled | polled over ICMP | both. **Category:** `<category>`. **Version:** `<x.y.z>`.

**What it reads:** the exact endpoints / exporter / OIDs / ICMP, and why each is read-only.

**Config**

| Field | Type | Default | Notes |
|---|---|---|---|

**Secrets:** which fields, and where the device wants them (header, SNMPv3 key, ...). "none" if none.

**Metrics:** names, types, labels, and what each means. For scraped exporters, the native metrics the cards use.

**Status and failures:** what makes the instance `down`, and what the admin sees in `error`.

**Firewall:** who connects to what, protocol and port, as in `firewall_hints()`.

**Cards:** `card_type`, title, default size, and the metrics each reads.

**Limits and caveats:** what the integration cannot see, device versions tried, anything the admin should know.

**Fixtures:** where they are, and how they were made (hand-written or scrubbed recording).
```

---

## 18. Built-in integrations

Index of per-integration docs, in `type_id` order. The first three are the reference implementations.

| `type_id` | Integration | Style | Doc |
|---|---|---|---|
| `http_check` | HTTP check | polled | [`integrations/http_check.md`](integrations/http_check.md) |
| `linux_host` | Linux host (node_exporter) | scrape-only | [`integrations/linux_host.md`](integrations/linux_host.md) |
| `pfsense` | pfSense (Telegraf) | scrape-only | [`integrations/pfsense.md`](integrations/pfsense.md) |
| `ping_check` | Ping check | polled over ICMP | [`integrations/ping_check.md`](integrations/ping_check.md) |
| `smart_exporter` | SMART disk health (smartctl_exporter) | scrape-only | [`integrations/smart_exporter.md`](integrations/smart_exporter.md) |
