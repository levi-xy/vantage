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

## Client address and trusted proxies

The login rate limit counts failures per username (5 per 15 minutes) and per client
address (50 per 15 minutes). The client address comes from one helper,
`backend/app/core/client_address.py` (used through `get_client_address` in
`backend/app/api/deps.py`), so everything that needs it agrees:

- **Trust is decided by the TCP peer only.** If the direct peer's address is in
  `VANTAGE_TRUSTED_PROXIES`, the client address is read from the `X-Real-IP` header.
  Otherwise the peer address is used and every forwarding header is ignored. Nothing a
  client puts in a header can make the backend trust it.
- **One header, overwritten by the proxy.** Only `X-Real-IP` is read. The bundled nginx
  sets it with `proxy_set_header X-Real-IP $remote_addr;`, which replaces any value the
  client sent. `X-Forwarded-For` is never used: nginx appends to it, so its first
  entries are whatever the client wrote.
- **Bad values fall back.** A missing header, a header sent more than once, or a value
  that is not an IP address means the peer address is used.
- **One spelling per client.** IPv4-mapped IPv6 (`::ffff:192.0.2.1`) is treated as
  IPv4 and IPv6 zone suffixes are dropped, both for the trust check and the limiter key.
- **Default: trust nobody.** Unset or empty, the client address is always the peer.
  Invalid entries (not a CIDR, host bits set) stop the backend at startup.
- **uvicorn's own proxy handling is off** (`--no-proxy-headers` in the backend image),
  so the peer address the app sees is the real TCP peer.

`deploy/docker-compose.yml` pins the compose network to `172.16.238.0/28`, gives the
frontend (nginx) the fixed address `172.16.238.2`, hands out every other container's
address from `172.16.238.8/29`, and sets `VANTAGE_TRUSTED_PROXIES=172.16.238.2/32`.
Only nginx can therefore name the client. If that range clashes with your network,
change the subnet, `ip_range`, the frontend's `ipv4_address` and
`VANTAGE_TRUSTED_PROXIES` together.

Limits:

- If you put another reverse proxy in front of the bundled nginx, nginx's
  `$remote_addr` is that proxy, so all clients share one per-client budget again (the
  per-username limit still applies). Vantage does not read a client address through a
  chain of proxies.
- Docker's userland proxy can make connections to a published port on the host's
  loopback (or over IPv6) appear to come from the network gateway; those clients then
  share one address.
- Never list a range that ordinary clients can reach the backend from: a trusted peer
  can claim any client address.

## Network guard

Every outbound connection to a device or cloud API goes through the SSRF guard in
`backend/app/core/net_guard.py` (Contract 5 in `AGENTS.md`). In short:

- A hostname is resolved once, every answer is checked, and the connection goes to
  the checked address, so DNS rebinding can't redirect it.
- Allowed by default: RFC 1918, RFC 6598 (CGNAT) and IPv6 ULA (`fc00::/7`), plus
  extra CIDRs an admin configures.
- Always denied: loopback, link-local, `0.0.0.0/8`, multicast, `240.0.0.0/4`,
  `fec0::/10`, `::/96`, cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`,
  `100.100.100.200`, `168.63.129.16`) and the Vantage host's own addresses.
  IPv4-mapped, NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`) and 6to4 forms of denied
  addresses are denied too.
- The public internet is reachable only for an integration's declared cloud hosts,
  over HTTPS with certificate verification, and a cloud host must resolve to public
  addresses only. For cloud hosts, NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`), 6to4
  (`2002::/16`) and `192.0.0.0/24` addresses are never treated as public, because a
  translated address can carry a private IPv4 address.
- Redirects are not followed automatically; timeouts are connect 5 s, total 15 s.

### Known limitation: the Docker host's LAN address

"The Vantage host's own addresses" are discovered from inside the process: the
addresses the hostname resolves to and the source address of the default route.
When Vantage runs in Docker, that is the **container's** address. The Docker host's
own LAN address (and the bridge gateway, e.g. `172.17.0.1`) are ordinary private
addresses from the container's point of view and are **not** denied automatically.
An admin who adds a device pointing at the Docker host can therefore reach services
listening on it. If that matters in your setup, bind host services to addresses the
container can't reach, or firewall the Docker bridge.
