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
  passed in.
- **Locked-out logins.** A login refused with 429 because the account is locked (its
  `login_failures` window is full) is audited as `login_failed` with the user as target
  and `changes` = `{"username": <stored username>, "locked": true}`, whether the password
  was right or wrong, so an admin can see that someone kept trying. The submitted
  username is never stored: a case variant such as `ADMIN` is recorded as the account it
  counted on. To keep a spray from filling the table, at most one such entry is written
  per account per minute (plain `login_failed` entries don't suppress it). This is best
  effort: parallel requests may each write one. Refusals by the per-client limit, and
  for usernames that match no user, are not recorded.
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
  `changes.failed_logins_cleared`, a count), a new user a `create`. The entry's
  `actor_type` is `cli` and its `user_id` is empty (`changes.source` is `"cli"` too,
  as in entries from before `actor_type` existed). The password is recorded only
  as `"<changed>"`; neither it nor its hash reaches the table, the output or any log.
  Error messages don't repeat what was typed (an unknown username, an unknown
  argument), because a mistyped command may hold a password.
- **What it loads.** The app's settings, database and audit writer. The user commands
  and `backup` don't load the master key, so a broken key file doesn't block recovery;
  only `restore` and `rotate-master-key` (below) need it.
  It never starts the web server, the poll scheduler or the collector, and never
  creates or migrates a database: it stops if the SQLite file or the tables are
  missing (the backend applies migrations when it starts).
- **Login lockouts.** `unlock <username>` clears the user's failed-login count, and so
  does `reset-password` (failures counted against the old password say nothing about
  the new one). Neither can clear the per-address limit, which is in the backend's
  memory: if logins from one address are still refused, `docker compose restart
  backend` clears it (see above).

## Backup, restore and master-key rotation

Run from `deploy/`. `-T` matters: it pipes stdin/stdout instead of a terminal.

```bash
# Backup: one gzip-compressed JSON file of every table.
docker compose exec -T backend python -m app.cli backup - > vantage-backup.json.gz
# or inside the data volume: ... python -m app.cli backup /data/backups/vantage.json.gz

# Restore into a freshly started (empty) Vantage, then restart the backend.
docker compose exec -T backend python -m app.cli restore - < vantage-backup.json.gz
docker compose restart backend

# Rotate the master key, with the backend stopped.
openssl rand -base64 32 > secrets/master.key.new
docker compose stop backend
docker compose run --rm -T backend python -m app.cli rotate-master-key --new-key-file - \
    < secrets/master.key.new
mv secrets/master.key secrets/master.key.old && mv secrets/master.key.new secrets/master.key
docker compose up -d --force-recreate backend
```

- **The master key is never in a backup.** Stored credentials stay encrypted in the file,
  so a backup without its key is useless for them. Back up `deploy/secrets/master.key`
  separately and keep it apart from the backup files; the CLI prints this reminder
  after every backup. The file still holds password hashes and the audit log: treat
  it as sensitive. A backup written to a path is created with mode `0600` and never
  overwrites an existing file; `backup -` refuses to write to a terminal.
- **Restore is one transaction and doesn't clobber.** It refuses a database that holds
  any row unless `--overwrite` is given (which deletes everything first). It parses the
  file strictly (only JSON, exactly Vantage's tables and columns, every value
  type-checked, enum columns limited to their allowed values, 1 GiB uncompressed cap)
  and only restores a backup from the same schema revision. A backup from an older
  version is refused in v0.1: restore it with the Vantage version that made it, then
  upgrade. It reports how many stored secrets decrypt with the server's key,
  and exits 1 (data restored) if some don't. A restored database carries its own key
  check value (see "Master key mismatch"), checked against the server's key: a backup
  whose secrets are on more than one key is refused (nothing changes); one made
  consistently with another key is restored, and the backend stays in key_mismatch mode
  until it runs with that key. Restore is audited (`target_type`
  `database`, `actor_type` `cli`). **Login sessions are never restored** (a session
  revoked after the backup, or a stolen cookie, would come back): everyone logs in again
  after a restore. The audit entry records how many were skipped.
- **Rotation is all or nothing.** `rotate-master-key` needs the current key (the one the
  backend runs with) and the new one. In one transaction, with the database locked
  against writes, it decrypts every `instances.secrets` and `channels.secrets` blob with
  the current key, re-encrypts it with the new one and reads everything back with the
  new key before committing. The key check value moves to the new key in the same
  transaction, so a backend still running on the old key goes into key_mismatch mode
  instead of failing on every secret. If any blob doesn't decrypt, nothing changes. It refuses
  to run if a table gains a binary column it doesn't know about. One audit entry
  (`target_type` `master_key`, `actor_type` `cli`) records only the per-table counts.
  Stop the backend first (`docker compose stop backend`) and run the command with
  `docker compose run --rm`, so nothing is written with the old key in between; then
  swap the key file (give the new one the same owner and mode as `master.key`) and
  start the backend. Keep the old
  key as long as you keep backups made before the rotation.
- **Keys and secrets are never printed**, logged or audited, and error messages never
  quote the backup's content.
- **Postgres.** The commands work on Postgres too (backup in a repeatable-read snapshot,
  restore and rotation under table locks, id sequences reset after a restore). For
  routine Postgres backups, `pg_dump` works as well; it carries the same encrypted
  blobs, and the master key still has to be backed up separately.

### Master key mismatch (`key_mismatch`)

The database holds a key check value: a `settings` row `key_check` with a fixed, known
value encrypted with the master key under its own context, so no other blob can stand
in for it. The backend decrypts it at startup and every 15 seconds. When that fails, the
key in `deploy/secrets/master.key` (or `VANTAGE_MASTER_KEY`) isn't the one the stored
device and channel credentials were encrypted with: the key file was replaced, lost, or
belongs to another installation, or a backup was restored with a different key.

The backend then runs in **key_mismatch mode** instead of refusing to start, so you can
still log in and see what's wrong:

- reads, logins and the UI keep working; a banner on every page says the master key
  doesn't match;
- polling, live rates and notification delivery stop (queued messages wait; they are
  sent once the key matches);
- every secret write (an instance or channel created or edited with credentials) is
  refused with `503 {"reason": "key_mismatch"}`, checked again inside each write's own
  transaction, so a database can never hold secrets under two keys;
- `/healthz` answers `503 {"reason": "key_mismatch"}` (first in its order), so an
  external monitor sees it even though no channel can send;
- the log repeats an ERROR line every 5 minutes, with per-table counts of the blobs that
  decrypt (never values);
- a system alert "Master key mismatch" is stored; its messages are recorded `failed`
  with `error_kind` `credentials_unreadable` (the channel credentials can't be decrypted
  either), never silently dropped.

On an upgrade from a version without the check value, it is created when every stored
secret decrypts with the current key (or none is stored). If none decrypt, the mode is
`wrong_key`; if only some do, `mixed_key` (the log names the per-table counts).

**To fix it**, put the right key back and restart: copy the key that belongs to this
database to `deploy/secrets/master.key` (same owner and mode as before) and run
`docker compose up -d --force-recreate backend`. The next check passes, the system alert
resolves and polling resumes. If the right key is lost, the stored credentials can't be
recovered: restore a backup together with its key, or delete and re-add the affected
devices and channels after starting over with an empty database. A `mixed_key` database
needs the key most secrets were written with, then the remaining ones re-entered.

## User management

Admins manage users in the UI (`/api/v1/users`); every user can change their own
password (`POST /api/v1/me/password`). The CLI above and the API share the same rules
(`backend/app/core/users.py`):

- **Passwords are write-only.** No response carries a password or its hash, and audit
  entries record only `"password": "<changed>"`.
- **Duplicate usernames are refused ignoring case**, by the same check as
  `create-user`. Usernames are stored as typed and compared exactly at login; the
  case-insensitive check is application code shared by the CLI and the API (there is
  no case-insensitive index). Only a real duplicate is reported as "already exists";
  any other database error is reported as an error.
- **A reset ends sessions.** An admin resetting a user's password ends all of that
  user's sessions and clears their failed logins. It writes the user row before it
  deletes sessions, like the CLI, so a racing login either fails or loses its session.
  Changing your own password follows the same order.
- **Changing your own password** needs the current one and ends your other sessions
  (this one stays). A wrong current password counts as a failed login on your
  account, so a stolen session can't guess it faster than the login form allows. The
  flip side: someone holding a stolen session can lock the account's password login
  this way. That's no new exposure, since anyone can already lock it with failed logins.
- **There is always an admin.** The last admin can't be demoted or deleted (409). The
  check runs after the change, in the same transaction, with the admin rows locked
  (Postgres), so two admins demoting each other at once can't leave none. An admin
  can't delete their own account; another admin has to.
- **A role change takes effect on the user's next request**; their sessions stay.
- **Audited.** Create, role change, reset, own password change and delete are each an
  entry with `actor_type` `user` and the acting admin (or user) as `user_id`. A failed
  login is a `user` entry with an empty `user_id` (nobody is logged in), and entries
  keep their place with an empty `user_id` once the acting user is deleted.

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

## Query proxy

Cards read metrics through `POST /api/v1/query` (any logged-in user, CSRF checked);
the browser never talks to Prometheus, which has no published port.

The backend reaches the bundled Prometheus at `VANTAGE_PROMETHEUS_URL` (default
`http://prometheus:9090`, the compose network) through `net_guard` with the default
policy, like every outbound connection. The compose network is RFC 1918, so the guard
allows it; a URL the guard refuses (loopback, for example) makes every query answer 503
`metrics backend refused by the network guard`, and the guard's reason is logged. On
top of the guard, the URL comes only from server config (an `http`/`https` URL without
credentials, checked at startup), the API paths are fixed, a request supplies only the
PromQL text and numbers (sent as form fields), redirects aren't followed and proxy
environment variables are ignored.

Budget (constants in `backend/app/query/proxy.py`); a query over it gets a 4xx with a
short message and never reaches Prometheus:

| Limit | Value |
|---|---|
| PromQL length | 2,000 characters |
| Range (`end - start`) | 31 days |
| Step | at least 15 s (default: range / 300, but at least 15 s) |
| Points per series | 1,100 |
| Series per result | 500 (Prometheus is asked for at most 501) |
| Prometheus' answer | 8 MiB |
| Time | 10 s evaluation timeout in Prometheus, 15 s for the whole call |

Prometheus' own error text is never passed on: the client sees `invalid PromQL query`
(400), `query could not be executed` (422), `query timed out` (504),
`metrics backend unavailable` (503) or `metrics backend error` (502).

## Alert channels (Telegram)

Alert channels are managed at `/api/v1/channels` (admin only, CSRF checked; the UI's
**Alerts** page). A Telegram channel stores a chat ID (plain config) and a bot token,
which is a secret like an instance's: encrypted with the master key into
`channels.secrets` (bound to the row), write-only (responses carry only
`secrets_set: {"bot_token": true|false}`, never the value or a masked fragment),
unchanged when a PATCH omits it, and recorded in the audit log only as `"<changed>"`.

Sending (`backend/app/alerts/channels.py`) is `POST
https://api.telegram.org/bot<token>/sendMessage` through `net_guard` with
`api.telegram.org` as the only declared cloud host, so the guard allows it only over
HTTPS with certificate and hostname verification, and only when every address it
resolves to is public. Redirects aren't followed, the guard's timeouts apply, and the
text is capped at Telegram's 4,096 characters and sent as plain text (no `parse_mode`).

Telegram puts the token in the URL path, so extra care keeps it out of logs and errors:

- The token's format is validated (`<digits>:<letters, digits, _ and ->`), so it can't
  add `/`, `?` or `#` and change which URL is requested.
- Send errors are built from fixed text, the exception type and Telegram's short
  `description`, then scrubbed of the token; an httpx exception's own text (which can
  hold the URL) is never used or chained.
- httpx logs every request URL at INFO level; a log filter on the `httpx` and
  `httpcore` loggers rewrites `/bot<token>` to `/bot<redacted>`.
- Alert messages wait in the `alert_notifications` outbox until delivered
  (`backend/app/alerts/delivery.py`). A failed attempt stores only a fixed `error_kind`
  code (`timeout`, `rate_limited`, `unauthorized`, ...), never Telegram's answer or the
  token; log lines carry the channel id, row ids, attempt count and that code. The stored
  message text is built from the alert's filtered labels and holds no secret. Delivery is
  at least once: a crash right after Telegram accepted a message can send it twice.

Recommended: give the bot only what it needs (a bot that can post to one chat), and allow
outbound HTTPS to `api.telegram.org` in your firewall only if you use Telegram alerts.

## Self-monitoring and /healthz

**The limitation first: if the Vantage backend or its host is down, Vantage can't tell
you.** Nothing below runs then. The same goes for the notification dispatcher itself being
stuck: its messages never leave, so only `/healthz` (`notifications_stuck`) shows it. Point an external monitor on another machine (Uptime
Kuma, cron + curl, a hosted dead-man's switch) at `https://<vantage>/healthz` and alert on
anything but 200. For example, every 5 minutes from another host:

```sh
curl -fsS -o /dev/null --max-time 10 https://vantage.example.com:8443/healthz \
    || echo "Vantage unhealthy" | mail -s "Vantage" admin@example.com
```

(Add `--cacert` with your certificate if it's self-signed, rather than `-k`.)

### What Vantage checks itself (`backend/app/alerts/selfcheck.py`)

| Failure | Detected by | After | Result |
|---|---|---|---|
| Prometheus unreachable (refused, timeout, HTTP 5xx) | the evaluator's per-tick probe | 2 min of failed probes | system alert "Prometheus unreachable" |
| Prometheus up but not scraping Vantage | age of the newest `up{job="vantage-backend"} == 1` | older than 2 min, or none for 2 min | system alert "Metrics not being collected" |
| Alert evaluation hung or crashed | a watchdog task: the evaluator's last progress (a query answered or failed, a tick finished), so a slow but working tick isn't a stall | 90 s (3 intervals) | system alert "Alert evaluation stopped"; the evaluator is restarted once |
| One rule's query keeps failing | per-rule counter | 3 ticks in a row | the rule shows the error on the Alerts page; no message (a configuration problem) |

A rule's own bad PromQL or too-many-series never counts as "Prometheus unreachable".
System alerts have no rule, open and resolve by themselves (they can't be closed by
hand), go through the same outbox to every channel with **Send Vantage's own alerts**
(`system_alerts`, on by default), and show as a banner in the UI while firing.

Shared failures send one message instead of a storm. While Prometheus is unreachable (or
not collecting, for alerts on polled devices) every affected alert records NO DATA with
that reason, but its message is stored *folded* into the system alert's message; when
Prometheus returns, still-true alerts go back to FIRING silently and the RESOLVED
message counts them ("5 alerts still firing, 2 recovering"). A device's other alerts are
folded into its instance-down message the same way ("3 other alerts on this device can't
be checked"), and while a device is failing but not yet marked down, its alerts' NO DATA
messages are held.
Outages shorter than 2 minutes change nothing. An intermittent Prometheus (down 2 minutes,
back for a tick, down again) gives a FIRING/RESOLVED pair about every 2.5 minutes; the
restart happens at most once until a tick really finishes again.

### `/healthz` and `/healthz/live`

Both are unauthenticated and carry nothing but a fixed code.

- `GET /healthz`: `200 {"status": "ok"}`, or `503 {"reason": "<code>"}` with exactly one
  code. When several apply, the first in this order wins: `key_mismatch` (the master
  key doesn't match the stored secrets, see "Master key mismatch"), `evaluator_stalled`
  (nothing else is being checked then), `prometheus_unreachable`, `metrics_not_collected`,
  `notifications_stuck` (an alert message undelivered for more than 15 minutes, or failed
  within 24 hours: the UI banner's rule). The conditions are kept in memory, so they start
  over when the backend restarts.
- `GET /healthz/live`: `200` whenever the web server runs. `setup.sh` waits on this one,
  so a fresh install whose Prometheus isn't up yet still finishes. The compose file has
  no container healthcheck; if you add one, use `/healthz/live`: a 503 from `/healthz`
  means "something Vantage depends on is down", and restarting the backend wouldn't fix
  it.
