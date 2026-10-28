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
address (50 per 15 minutes). An IPv6 client counts as its whole /64, since one host
usually controls a /64 and could otherwise rotate addresses for fresh budgets.

**Per-user counts for existing accounts are in the database** (`login_failures`, one
row per failed attempt: a user id and a time, nothing else). The limit is a sliding
15-minute window over those rows. There is no cap on how many accounts are tracked, so
no spray of other usernames can push an account's count out, and lockouts survive a
restart. Each attempt's row is committed before the password check, so parallel
guesses can't get past the limit; a refused attempt (429) deletes its own row, and rows
that left the window are pruned on the user's next attempt. A username that differs
from an account's only in case counts on that account (login still compares names
exactly), so case variants behave the same for existing and unknown names. A successful
login clears the user's rows.

Usernames that match no account, and client addresses, are counted in memory. The
in-memory limiter tracks at most 10,000 keys. When it is full it drops expired keys,
then keys still under their limit with the most attempts left (limit minus recent
failures; least recently used first among equals), 5% of the table per scan. It never
drops a key that is locked out. If too few keys can be dropped, new login attempts are
refused with 429 (fail closed) until the first lockout ends, at most 15 minutes. That
result is cached, so refused attempts don't rescan the table. Already logged-in sessions
are not affected. A large enough spray (about 38,000 attempts from about 760 addresses
in simulation) can still push out a near-limit key here, but those are only junk
usernames and client addresses: a real account's count is not in this table.

The in-memory limiter is cleared by **restarting the backend**; database lockouts are
not. A legitimately locked-out user gets back in right away with
`docker compose exec backend python -m app.cli unlock <username>` (see the admin CLI
below). Being caught by the per-address limit or the fail-closed state needs
`docker compose restart backend`. Anyone who can do either already has shell access to
the host, so this gives nothing to a network attacker.

On a LAN reached over IPv6, every host in one /64 shares one per-client budget, the
same way hosts behind one NAT address share one on IPv4.

The client address comes from one helper,
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

`deploy/docker-compose.yml` pins the compose network to `192.168.255.240/28` with the
gateway at `192.168.255.241`, gives the frontend (nginx) the fixed address
`192.168.255.242`, hands out every other container's address from `192.168.255.248/29`,
and sets `VANTAGE_TRUSTED_PROXIES=192.168.255.242/32`. Only nginx can therefore name the
client. The gateway is pinned because Docker versions differ when it is left out
(Docker 29.1 picked `192.168.255.248`, from inside `ip_range`), so the address
host-loopback clients appear to come from would depend on the Docker version.

Why this range: a fixed subnet can clash with a network the host already uses. Docker's
own compose examples use `172.16.238.0/24`, and the low `172.16.0.0/12` and `10.0.0.0/8`
ranges are common on corporate LANs and VPNs. `192.168.255.240/28` is a small block at
the very top of `192.168.0.0/16`, where home routers rarely put their LANs, and Docker
only reaches that part of its default pools after about 30 other networks. If it still
clashes, `docker compose up` fails with "Pool overlaps with other one on this address
space", or the backend can't reach devices in that range. To fix it, pick another
unused /28 and change these together in `deploy/docker-compose.yml`: the network's
`subnet` and `ip_range` (the upper half of the subnet), its `gateway` (the first usable
address of the subnet), the frontend's `ipv4_address` (another address in the lower
half), and the `VANTAGE_TRUSTED_PROXIES` default (that address as a /32).

Changing the network of an existing install: run `docker compose down`, then
`docker compose up -d` (volumes, and so the data, are kept). A plain `docker compose up -d`
replaces the network but reconnects the running containers without the frontend's fixed
address: nginx then keeps restarting with "host not found in upstream". The same applies
once to installs created before the gateway was pinned. If that already happened,
`docker compose up -d --force-recreate` fixes it.

Limits:

- If you put another reverse proxy in front of the bundled nginx, nginx's
  `$remote_addr` is that proxy, so all clients share one per-client budget again (the
  per-username limit still applies). Vantage does not read a client address through a
  chain of proxies.
- With the default `VANTAGE_BIND_ADDRESS=127.0.0.1`, everything reaching the dashboard
  through the host's loopback (a browser on the host, an SSH tunnel, or an HTTPS reverse
  proxy running on the host) arrives through Docker's userland proxy and appears to
  come from the network gateway (`192.168.255.241`). All of those clients share one
  per-client budget. The same can happen for connections over IPv6. The per-username
  limit still applies to each account.
- Never list a range that ordinary clients can reach the backend from: a trusted peer
  can claim any client address.

## Recovering a lost admin password

Setup only works while no users exist, so a lost password is recovered with the admin
CLI, run on the server from the `deploy/` directory:

```bash
docker compose exec backend python -m app.cli reset-password <username>
```

It asks for the new password twice, at a hidden prompt, sets it, and ends **all** of
that user's sessions, so every browser still logged in as them is logged out on its
next request. The same CLI has:

```bash
docker compose exec backend python -m app.cli create-user <username> --role admin
docker compose exec backend python -m app.cli list-users
docker compose exec backend python -m app.cli unlock <username>
```

`list-users` shows id, username, role, created and last-login times, never hashes.

- **No login needed.** Shell access to the host is the trust boundary: anyone who can
  run `docker compose exec` can already read the database and the master key.
- **Passwords never on the command line.** There is no password argument or
  environment variable, since both end up in shell history or process listings. For
  scripts, `--password-stdin` reads one line from stdin instead (use
  `docker compose exec -T` so stdin is piped). The CLI refuses rather than read a
  password with echo on: `--password-stdin` on a terminal, or a prompt without one.
- **A reset beats a racing login.** Login only writes its session if the user's hash
  is still the one it just verified (a conditional update of the user row). A login
  that checked the old password while a reset was committing gets 401, and a password
  rehash on login can never overwrite the new hash. A login that committed first has
  its session deleted by the reset.
- **Same rules as setup and login.** Usernames match the setup wizard's pattern and are
  stored as typed; login compares them exactly, so `reset-password` does too.
  `create-user` refuses a name that differs from an existing one only in case, since
  a failed login with either counts on the existing account. It warns when no admin exists afterwards,
  since browser setup is then closed. Passwords are 12 to 1024 characters and are
  hashed with the same Argon2 parameters as login.
- **Audited.** A reset or an unlock is an `update` on the user (with
  `changes.failed_logins_cleared`, a count), a new user a `create`. The actor
  (`user_id`) is empty and `changes.source` is `"cli"`. The password is recorded only
  as `"<changed>"`; neither it nor its hash reaches the table, the output or any log.
  Error messages don't repeat what was typed (an unknown username, an unknown
  argument), because a mistyped command may hold a password.
- **What it loads.** The app's settings, database and audit writer. Not the master key:
  the CLI touches no encrypted data, and a broken key file shouldn't block recovery.
  It never starts the web server, the poll scheduler or the collector, and never
  creates or migrates a database: it stops if the SQLite file or the tables are
  missing (the backend applies migrations when it starts).
- **Login lockouts.** `unlock <username>` clears the user's failed-login count, and so
  does `reset-password` (failures counted against the old password say nothing about
  the new one). Neither can clear the per-address limit, which is in the backend's
  memory: if logins from one address are still refused, `docker compose restart
  backend` clears it (see above).

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
