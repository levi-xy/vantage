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
- ICMP ping goes through the same guard (`GuardedPingClient`): the host is resolved
  once, every answer is checked, and only the checked address is pinged. A declared
  cloud host is never pinged, so ping reaches allowed private ranges only. At most 10
  echo requests per run, at least 0.2 s apart, and every run ends within 15 s; DNS
  gets at most 5 s of that. The client uses unprivileged ICMP datagram sockets,
  connected to the checked address and without `SO_BROADCAST` (so the kernel refuses
  a broadcast address it knows about). A reply counts only if it comes from the
  checked address and echoes the run's random nonce and the expected sequence number.
  Integration code never opens sockets. The compose file sets the
  `net.ipv4.ping_group_range` sysctl on the backend
  container (its own network namespace only), so the backend keeps running as a
  non-root user with every capability dropped, `NET_RAW` included.

### Known limitation: the Docker host's LAN address

"The Vantage host's own addresses" are discovered from inside the process: the
addresses the hostname resolves to and the source address of the default route.
When Vantage runs in Docker, that is the **container's** address. The Docker host's
own LAN address (and the bridge gateway, e.g. `172.17.0.1`) are ordinary private
addresses from the container's point of view and are **not** denied automatically.
An admin who adds a device pointing at the Docker host can therefore reach services
listening on it. If that matters in your setup, bind host services to addresses the
container can't reach, or firewall the Docker bridge.

The same applies to ICMP ping: a ping check can target the Docker host's LAN address
or the bridge gateway (an address in `172.16.0.0/12`, such as `172.17.0.1`). The harm
is low, since an echo reply only shows that the address is up and how long the round
trip took, and carries no data from the host, but it does let an admin confirm those
addresses exist.
