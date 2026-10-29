# Emby (`emby`)

**Style:** polled (HTTP or HTTPS API). **Category:** `media`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/emby/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** the Emby Server REST API, `GET` only, through `ctx.http_client()` (the SSRF-guarded client: the host is resolved once, checked, and only the checked address is connected to). Every request goes through `_get_json`, which refuses any target not on the allowlist (`ALLOWED_PATHS`) before anything is sent:

| Request | When | Why it's read-only |
|---|---|---|
| `GET /emby/System/Info` | connection test and poll | server version, update and restart flags |
| `GET /emby/Sessions?ActiveWithinSeconds=960` | connection test and poll | the list of active sessions and what each is playing |
| `GET /emby/Items/Counts` | poll (optional) | item totals by kind |
| `GET /emby/Library/VirtualFolders` | poll (optional) | the list of libraries and their types |

The two optional requests are left out of the poll, silently, if the server refuses them (for example 403) or answers something unexpected. Nothing else is called: no `POST`/`PUT`/`DELETE`, no login (`Users/Authenticate`), no playback command (`Sessions/{id}/Playing`, `/Message`, `/Command`), no library refresh, no restart or shutdown, no item or user endpoint. Redirects are not followed.

**Config:** see `EmbyConfig`.

- `host` is the Emby server (IP literal preferred); it must be on a private network (the SSRF guard decides). Stored in plaintext.
- `port` defaults to 8096 (Emby's http port); Emby's https default is 8920.
- `use_https` is off by default because Emby serves plain http out of the box. With http the API key (in the `X-Emby-Token` header) **is sent unencrypted**, and a successful connection test says so ("API key sent over plain http; enable HTTPS on the server to protect it"); use HTTPS (Emby's own certificate, or a reverse proxy in front) wherever you can.
- `api_key` is the secret; see below.
- `tls_ca_certificate` and `tls_insecure_skip_verify` apply to HTTPS only (the form refuses them when `use_https` is off, since there is nothing to verify, and refuses both together; plain http itself is allowed). Verification is **on** by default. For a self-signed or private-CA certificate, paste that certificate in **Trusted certificate (PEM)**: only it is trusted, and the host must still match it. **INSECURE: don't verify the TLS certificate** is a last resort that sends the key to whatever answers. The PEM field accepts certificates only (never a private key), at most 16 KiB.
- `timeout_seconds` applies to each request (at most 15 s).

**Secret and least privilege:** `api_key`, a write-only `SecretStr`. It is sent only in the `X-Emby-Token` header, to the configured host and port; never in a query string, a message, a log line, a metric or an API response.

Emby API keys (Dashboard, Advanced, API Keys) cannot be limited to read-only: Emby gives a key the rights of a server administrator. So the safety comes from two things: Vantage sends only the four `GET`s above (enforced in code, tested), and you give it a key of its own. Create a key named "Vantage" used by nothing else, so you can revoke it without touching other apps, rotate it when you like, and see in Emby's activity which calls it makes. Keep the server on a private network, and prefer HTTPS so the key is not readable on the wire.

**Privacy:** the metrics are counts. User names, device and client names, client addresses and media titles are never read into a label or a message, and neither are server, library and path names.

**Metrics:** prefix `vantage_emby_`. A server that is down or refuses the key marks the instance `down` (`vantage_instance_up` 0); there is no separate `up` series.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `server_info` | gauge, 1 | `version` | the server version, only if it looks like one (digits, letters, `.+_-`, at most 32 characters), else empty |
| `update_available` | gauge | none | 1 if Emby reports a newer server version |
| `restart_pending` | gauge | none | 1 if the server needs a restart |
| `sessions` | gauge | none | sessions active in the last 16 minutes (up to 2000) |
| `now_playing` | gauge | none | sessions that are playing something |
| `streams` | gauge | `play_method` | playing sessions by `transcode`, `direct_stream`, `direct_play` or `unknown`; all four are always present |
| `items` | gauge | `item_type` | totals by `movie`, `series`, `episode`, `album`, `song`, `artist`, `music_video`, `box_set`, `trailer`, `program`, `game`, `book`; a kind the server doesn't report has no series |
| `libraries` | gauge | `library_type` | libraries by collection type (`movies`, `tvshows`, `music`, ...), plus `other` for a library without one |

All label values come from fixed lists in the code (or the validated version), never from the server's text.

**Status and failures:** the info and sessions requests must succeed; otherwise the instance goes `down` with a short, fixed message that names only `<host>:<port>`: key refused (401), not allowed (403), certificate not trusted, could not connect, timed out, `answered HTTP <status>`, "answered, but not like the Emby API" (wrong port, wrong http/https choice, something in between, a body over 4 MiB or a shape that isn't Emby's), `refused by the network guard: ...`, or a poll over its own 50 s budget.

**Firewall:** `tcp` to the configured port on the Emby server, from the Vantage backend, for polls and connection tests.

**Cards:** `emby-overview`, "Emby", 4 by 3: `now_playing`, `sessions`, `streams` by method, `server_info`, `update_available`. `emby-libraries`, "Emby libraries", 4 by 2: `items` and `libraries`. The card components are frontend work (M5.5).

**Open questions:** `TODO(maintainer)`: the server's name as a label on `server_info` would help dashboards but is free-form text, and the label exception in INTEGRATIONS.md section 9 does not list Emby. It is not exported until the maintainer decides.

**Caveats:** `now_playing` counts sessions with a `NowPlayingItem`, including paused ones. Field names come from the Emby API documentation and hand-written fixtures; not yet verified against a live server. Fixtures: `backend/tests/integrations/builtin/fixtures/emby_*.json`, example values only.
