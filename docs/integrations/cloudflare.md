# Cloudflare account (`cloudflare`)

**Style:** polled (cloud HTTPS API). **Category:** `cloud`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/cloudflare/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

Per-zone HTTP traffic for one Cloudflare account (requests, cache, bytes, status codes, countries, threats) and the status of its Cloudflare Tunnels. It reads the account through Cloudflare's API. For a `cloudflared` connector's own metrics, use [`cloudflared`](cloudflared.md).

**What it reads:** the Cloudflare API v4 at `api.cloudflare.com` and nothing else. That is the integration's only `cloud_hosts` entry. The SSRF guard lets it through only over HTTPS, with certificate and hostname verification on, and only when every DNS answer is a public address. A private, CGNAT or ULA answer is refused. Requests go through `ctx.http_client()`, and redirects are not followed. `_request` refuses anything not on this list before it is sent:

| Request | When | Token permission | Why it's read-only |
|---|---|---|---|
| `GET /client/v4/zones?account.id=<account>&per_page=50&page=1&order=name&direction=asc` | connection test and poll | Zone: Zone: Read | lists the account's zones |
| `GET /client/v4/accounts/<account>/cfd_tunnel?is_deleted=false&per_page=100&page=1` | connection test and poll, if **Tunnel status** is on | Account: Cloudflare Tunnel: Read | lists the tunnels and their connections |
| `POST /client/v4/graphql` with `ZONE_TRAFFIC_QUERY` | connection test (first zone) and poll, if **Zone traffic analytics** is on | Zone: Analytics: Read, Account: Account Analytics: Read | a GraphQL `query` (see below) |

**The GraphQL POST.** This is a maintainer-approved exception to Hard rule 2, dated 2026-10-29 and limited to this integration ([INTEGRATIONS.md](../INTEGRATIONS.md) section 7). The GraphQL Analytics API can only be read with a `POST`. The old `GET` zone-analytics endpoint has been retired. The guard rails, each with a test:

- There is one path, `/client/v4/graphql`. A `POST` to anything else is refused.
- The body is `{"query", "variables"}` and nothing else.
- The query must be a document from `QUERY_DOCUMENTS`, a constant in the code. Today that is only `ZONE_TRAFFIC_QUERY`. It must also pass `is_single_query`: exactly one `query` operation, no `mutation`, `subscription` or `fragment`, no string literals, one top-level block. Any other document is refused before sending, even a valid read-only query, so no mutation or subscription can ever go out.
- Variables are limited to `zoneTags` (1 to 10 zone IDs, 32 hex digits each), `start` and `end` (whole UTC hours) and `limit` (1 to 10). Values never go into the document text.
- The read-only token below couldn't change anything even if a mutation reached Cloudflare.

`PUT`, `PATCH` and `DELETE` are never sent. Neither is any other path: no DNS records, cache purge, settings, token or member endpoints.

**Config:** see `CloudflareConfig`. There is no host field: the API host is fixed.

- `account_id`: the 32-character account ID (Cloudflare dashboard, Account home, or the right-hand column of a zone's Overview). Stored in plaintext and shown to every user. It identifies the account but is not a credential.
- `include_analytics` (**Zone traffic analytics**) and `include_tunnels` (**Tunnel status**): both on by default. Turn one off if the token lacks that permission.
- `max_zones`: how many zones are reported (the first ones by name, from the first page of 50).
- `top_countries` and `top_status_codes`: how many countries and status codes each zone reports by name. The rest are summed as `other`. See Labels.
- `timeout_seconds`: applies to each request (at most 15 s, the guarded client's own limit).

**Secrets:** `api_token`, a write-only `SecretStr`. It is sent only in the `Authorization: Bearer <token>` header, to `api.cloudflare.com`. It never appears in a URL, a GraphQL body, a message, a log line, a status, a metric or an API response. The validator refuses a token with spaces or control characters without echoing it.

### Creating a read-only token

In the Cloudflare dashboard, go to My Profile, API Tokens, Create Token, then **Create Custom Token**, and give it only these permissions:

| Scope | Permission | Access |
|---|---|---|
| Account | Account Analytics | Read |
| Account | Cloudflare Tunnel | Read |
| Zone | Analytics | Read |
| Zone | Zone | Read |

Under **Account Resources**, include only this account. Under **Zone Resources**, include all zones of this account (or specific zones; zones the token can't see are simply missing). Optionally restrict **Client IP Address Filtering** to the egress address of the Vantage host, and set a TTL.

Don't use the **Global API Key**, or a token with any **Edit** permission or one that covers other accounts. A broader token works, but if it leaked from the Vantage host it could change your DNS, firewall and tunnels. Vantage needs none of that. An account-owned token (Manage Account, API Tokens) with the same permissions works too.

**Metrics:** prefix `vantage_cloudflare_`, all gauges. The constants are at the top of `integration.py`.

| Metric | Labels | Meaning |
|---|---|---|
| `zones`, `zones_omitted` | none | zones in the account (Cloudflare's total count); how many weren't reported (beyond `max_zones`) |
| `zone_info` | `zone_id`, `zone_name` | value 1; the only series with the zone's name |
| `zone_active` | `zone_id` | 1 if the zone's status is `active` |
| `zone_paused` | `zone_id` | 1 if the zone is paused (Cloudflare only resolves DNS for it) |
| `analytics_up` | none | 1 if every analytics query of the poll succeeded; 0 and the traffic series below are missing (or partial) otherwise |
| `analytics_window_end_timestamp_seconds` | none | end of the hour the traffic values cover |
| `zone_requests`, `zone_cached_requests` | `zone_id` | requests, and requests served from cache, in the window |
| `zone_bytes`, `zone_cached_bytes` | `zone_id` | bytes sent to clients, and the part served from cache, in the window |
| `zone_threats` | `zone_id` | requests Cloudflare classified as threats, in the window |
| `zone_page_views` | `zone_id` | page views, in the window |
| `zone_requests_by_status_class` | `zone_id`, `status_class` | requests per `1xx`…`5xx`, plus `other` (an edge status outside 100–599); always all six |
| `zone_requests_by_status` | `zone_id`, `status` | requests for the `top_status_codes` busiest codes (`"200"`, …), the rest as `other` |
| `zone_requests_by_country`, `zone_threats_by_country` | `zone_id`, `country` | requests and threats for the `top_countries` countries with the most requests (ISO 3166-1 alpha-2, plus Cloudflare's `XX` unknown and `T1` Tor), the rest (and anything that isn't a two-character code) as `other` |
| `tunnels`, `tunnels_omitted` | none | non-deleted tunnels in the account; how many were beyond the first 100 |
| `tunnel_info` | `tunnel_id`, `tunnel_name` | value 1; the only series with the tunnel's name |
| `tunnel_healthy` | `tunnel_id` | 1 if Cloudflare reports the tunnel `healthy` |
| `tunnel_status` | `tunnel_id`, `status` | one series per `healthy`, `degraded`, `down`, `inactive`; 1 for the current one (all 0 for a status Cloudflare adds later) |
| `tunnel_connections` | `tunnel_id` | connections from `cloudflared` to Cloudflare's edge |

**The analytics window** is the last complete UTC hour (`httpRequests1hGroups`, available on every plan). At 11:23 UTC the values cover 10:00 to 11:00. They are totals for that hour, not counters, so use them directly (`zone_requests`) instead of `rate()`. Cloudflare can still fill in the last hour for a few minutes after it ends, so the first poll after the hour can read slightly low. A zone with no traffic reads 0. A zone the token can't read analytics for has no traffic series.

**Labels:** values series carry only stable IDs: `zone_id` (32 hex digits), `tunnel_id` (a UUID), and the bounded `status_class`, `status` and `country`. Per zone, that is at most `top_status_codes` + 1 status series, `top_countries` + 1 country series of each kind, and 6 status classes. With the largest caps (50 zones, 25 countries, 20 status codes, 100 tunnels), a poll stays at about 5,000 samples, under the collector's 10,000. IDs that don't have the expected form are skipped, never used as a label. Zone names (domains, but entered by the admin) and tunnel names (admin text) appear only on `zone_info` and `tunnel_info`, cleaned and capped at 128 characters (`ifmib.clean_text`). This is the maintainer's label exception for `cloudflare` (2026-10-29, INTEGRATIONS.md section 9). Dashboards join them on `zone_id` / `tunnel_id`.

**Rate limits.** Cloudflare allows about 1,200 REST requests per 5 minutes per user (across all of that user's tokens) and limits GraphQL queries per user and per query cost. Vantage stays far below that:

- The poll interval is 300 s. One poll is two REST requests plus one GraphQL query per 10 zones: at most 7 requests per 5 minutes, at the largest zone cap.
- A GraphQL query asks for at most 10 zones and one hourly bucket per zone.
- An HTTP 429 from any request, or a GraphQL error that says the rate limit or query budget is used up, stops all requests of that instance for the `Retry-After` the API sent. That is whole seconds, kept between 1 s and 1 hour; when Cloudflare sends no `Retry-After`, or an HTTP date, the pause is 300 s. Polls and connection tests during the pause send nothing. The pause is in memory: a restart clears it.

Several instances with the same token share Cloudflare's per-user budget but pause independently.

**Status and failures:** the instance goes `down` with these `error` texts (written by the integration; no Cloudflare error text is ever shown):

| What happened | `error` |
|---|---|
| zone list answered 400/401/403 | `Cloudflare API refused the API token (HTTP 403): check that it is active and not expired, and that it has Zone Read on this account` |
| tunnel list answered 403 | `the API token can't read Cloudflare Tunnels (HTTP 403): add the Cloudflare Tunnel Read permission, or turn off 'Tunnel status'` |
| tunnel list answered 404 | `Cloudflare API answered HTTP 404 for the account's tunnels: check the account ID` |
| HTTP 429 | `Cloudflare API rate limit reached: Vantage sends no request for <n> s (as the API asked)`, then `Cloudflare API rate limit: waiting <n> s more before the next request` |
| another status | `Cloudflare API at api.cloudflare.com:443 answered HTTP <status>` |
| not the API's JSON (a proxy, a captive portal) | `api.cloudflare.com:443 answered, but not like the Cloudflare API (is something in between, such as a proxy?)` |
| TLS verification failed | `TLS handshake with api.cloudflare.com:443 failed: its certificate could not be verified (is a TLS-intercepting proxy in the way?)` |
| timeout, refused | `timed out waiting for api.cloudflare.com:443`, `could not connect to api.cloudflare.com:443 (refused or unreachable)` |
| DNS answer not public | `refused by the network guard: cloud host resolved to non-public address <address>` |
| poll over 50 s | `Cloudflare poll took longer than 50 s (...)` |

A wrong account ID with a valid token doesn't fail the zone list: Cloudflare answers with an empty list, so the connection test says `0 zones`. If the analytics query fails with anything other than a rate limit, the instance stays `up`, `analytics_up` is 0, and the backend logs a warning with the instance ID only. The connection test reports it as `the API token can't read zone analytics: ...`.

**Firewall:** the Vantage backend connects out to `api.cloudflare.com`, TCP 443 (HTTPS). That is the hint `firewall_hints()` returns, and the one a timeout or refused connection shows. Cloudflare's API addresses are public and can change: allow the name if your firewall can, otherwise Cloudflare's published ranges. A TLS-intercepting proxy breaks the connection by design: verification can't be turned off for a cloud host.

**Cards:** `cloudflare-zones` ("Cloudflare zones", 4×3): `zone_requests`, cache ratio (`zone_cached_requests / zone_requests`), status classes, top countries and threats per zone, named through `zone_info`. `cloudflare-tunnels` ("Cloudflare tunnels", 4×2): `tunnel_status` and `tunnel_connections`, named through `tunnel_info`. Both cards are built in M5.5.

**Limits and caveats:**
- Only the first page of zones (50, by name) is read. An account with more zones reports `zones_omitted`. Choosing specific zones isn't supported yet.
- At most 100 tunnels (one page).
- Hourly resolution only. Finer datasets (`httpRequestsAdaptiveGroups`) depend on the plan and aren't used.
- Not verified against a live Cloudflare account. The response shapes, the GraphQL field names and the GraphQL rate-limit error text follow Cloudflare's documentation, and the fixtures are hand-written from it. In particular, check whether Cloudflare accepts 10 zones per `zoneTag_in` query, and the exact text of its GraphQL budget error.

**Fixtures:** `backend/tests/integrations/builtin/fixtures/cloudflare_zones.json`, `cloudflare_tunnels.json` and `cloudflare_graphql_zone_traffic.json`. They are hand-written in the shape of Cloudflare API v4 answers, with example values only: `example.com`, `example.net` and `example.org` zones, made-up hex and UUID identifiers, and a TEST-NET-1 origin address.
