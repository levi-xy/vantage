# HTTP check (`http_check`)

**Style:** polled. **Category:** `generic`. **Version:** `0.1.0`. Code: `backend/app/integrations/builtin/http_check/integration.py`. Guide: [INTEGRATIONS.md](../INTEGRATIONS.md).

**What it reads:** one `GET` of the configured URL per poll, through `ctx.http_client()`. Only the response headers are read (the body is streamed and never downloaded), and redirects are not followed, so the bearer token only ever goes to the configured URL. HTTP only; ICMP is `ping_check`.

**Config:** see `HttpCheckConfig` for fields, defaults and bounds.
- `url` is found by name by the save-time check. It must not carry credentials, and everything in it, including the query string, is stored in plaintext and shown to every user.
- `verify_tls` defaults to on; turn it off only for a self-signed certificate.
- `timeout_seconds` bounds the whole request and is capped at the guarded client's 15 s.

**Secrets:** `bearer_token` (optional), sent as `Authorization: Bearer` to the configured URL only, and only to an `https://` URL: a token with an `http://` URL is refused on save, because it would cross the network readable.

**Upgrading (v0.1, M8.3):** an existing check saved with an `http://` URL **and** a bearer token stops working after the upgrade, on purpose: the token is no longer sent in cleartext. The device goes `down` with the error "a bearer token is only sent to an https:// URL: change the URL to https:// or remove the token", and **Test** and **Save** in its edit form show the same reason. Fix it in the edit form: change the URL to `https://` (turn off `verify_tls` only for a self-signed certificate), or clear the token if the endpoint doesn't need it. The token itself is never shown.

**Metrics** (gauges, constants `METRIC_*` in the source): `vantage_http_check_success`, `vantage_http_check_status_code`, `vantage_http_check_duration_seconds` (time to the response headers).

**Status and failures:** no response (timeout, refused, TLS failure, guard refusal) raises, so the instance goes `down` (`poll timed out`, `refused by the network guard: ...`, or `poll failed (<Type>)`). A response with the wrong status is a result, not a failure: `success` is 0, the status code is kept, and the instance stays `up`.

**Firewall:** TCP from the backend to the URL's port (80 or 443 unless the URL says otherwise).

**Cards:** `http-check`, "HTTP check", 2 by 2.

**Limits and caveats:** the check says nothing about the body of the response.

**Fixtures:** none; responses are built in the test with `http_response()`.
