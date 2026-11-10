# Running Vantage

How to run, expose, upgrade and back up a Vantage install, and how its device and alert timing works. Start with the [Quick start](../README.md#quick-start); security details are in [SECURITY.md](SECURITY.md).

## Reaching the dashboard from other machines

The dashboard listens on every address of the server, so it's reachable from your LAN at `https://<server-address>:8443`; `setup.sh` prints the address and puts it in the generated certificate. To listen on one network only, set `VANTAGE_BIND_ADDRESS` in `.env` (in the folder you ran `setup.sh` from) to the server's address on it, or to `127.0.0.1` for the server alone (behind a reverse proxy on the host), then run `docker compose up -d`. Add any other names you use for the server to `VANTAGE_TLS_NAMES` before the first run, so the generated certificate covers them. Give the server a fixed address (a DHCP reservation): the certificate holds the address it had at setup.

An install from before 0.1.0-rc.4 has `VANTAGE_BIND_ADDRESS=127.0.0.1` in its `.env` and is only reachable from the server itself. To open it to the LAN, set `VANTAGE_BIND_ADDRESS=0.0.0.0`, delete `certs/tls.crt` and `certs/tls.key`, and run `./setup.sh` again (it keeps the key, `.env` and data).

Never publish the dashboard to the internet. Recommended: a dedicated monitoring VLAN, outbound internet blocked except the cloud API hosts you use, and dashboard access only from admin machines or your VPN.

## HTTPS certificate

The bundled nginx serves the dashboard over HTTPS (TLS 1.2+) from `certs/tls.crt` and `certs/tls.key`; plain HTTP to the same port is redirected. Browsers warn about the generated self-signed certificate until you accept it.

To use your own certificate (from your internal CA or a public one):

1. Put the PEM certificate, full chain, in `certs/tls.crt` and its unencrypted PEM key in `certs/tls.key`. `setup.sh` never replaces existing files.
2. Run `./setup.sh` again (it fixes the key's ownership and mode so only the frontend container can read it), then `docker compose restart frontend`.

To regenerate the self-signed pair (for example after changing `VANTAGE_TLS_NAMES`), delete both files and run `./setup.sh`.

Once browsers trust your certificate you can turn on HSTS with `VANTAGE_HSTS=true`. Plain HTTP is an explicit opt-out: `VANTAGE_HTTPS=false`, plus `VANTAGE_COOKIE_SECURE=false` for logins from other machines. Anyone on the network path can then read passwords and session cookies.

## Upgrading

Back up first (see below), then:

- **From a release:** download the new bundle and unpack it next to the old one. Copy your `secrets/`, `certs/` and `.env` from the old folder (never the other files: the new compose file belongs to the new release). In the new folder run `docker compose down`, then `./setup.sh`. If the release notes say the compose file didn't change, you can instead set `VANTAGE_VERSION=<new version>` in `.env` and run `docker compose pull && docker compose up -d`.
- **From source:** `git pull`, then `docker compose up -d --build` in `deploy/`.

Database migrations run by themselves when the backend starts. When a release changes the compose network (the release notes say so), run `docker compose down` before `up -d`: a plain `up -d` leaves the frontend without its fixed address and the dashboard down. The [CHANGELOG](../CHANGELOG.md) lists each version's upgrade notes.

## Backups

```bash
docker compose exec -T backend python -m app.cli backup - > vantage-backup.json.gz
```

This runs while Vantage is up. The master key (`secrets/master.key`) is not in the backup: keep its own copy somewhere off the server. Without it, stored device credentials can't be decrypted.

Restoring (`app.cli restore`) and master-key rotation (`app.cli rotate-master-key`, which re-encrypts every stored secret with a new key) are offline. Stop the backend first (`docker compose stop backend`) and run them with `docker compose run --rm -T backend python -m app.cli ...`. They refuse while the backend runs, and the backend doesn't start while one of them runs. Details: [SECURITY.md](SECURITY.md#backup-restore-and-master-key-rotation).

## Monitoring Vantage itself

Vantage alerts when Prometheus is unreachable, isn't collecting metrics, or alert evaluation stops, and says why. It can't tell you when Vantage itself is down: if the backend or its host is down, or the notification dispatcher is stuck, no message is sent.

Point an external monitor on another machine (Uptime Kuma, cron + curl, a hosted dead-man's switch) at `https://<vantage>/healthz` and alert on anything but 200. See [Self-monitoring](SECURITY.md#self-monitoring-and-healthz).

## Device down detection

A polled device is marked down after 3 failed poll cycles in a row (one quick retry per cycle), so a single timeout doesn't flip it. At the default 60 s interval that takes about 2–4 minutes; at 5-minute intervals, 10–16 minutes. Change it with `VANTAGE_DOWN_AFTER_FAILED_CYCLES` (1 to 10) in `.env`. The timing is explained in [INTEGRATIONS.md](INTEGRATIONS.md#poll-cycles-the-retry-and-down).

## Alert channels

Alerts go to Telegram. Admins add a channel (bot token and chat ID) under **Settings → Notification channels** and can send a test message from there. The bot token is stored encrypted and never shown again. Alert rules are managed on the **Alerts** page.

## How alerts resolve

An alert resolves only after two clean checks on two different polls, where the device's value was seen and the condition was false. A missing value (device down, metric gone, query failing, removed from service discovery) never counts as recovery: the alert moves to **No data** and stays open until the value returns or an admin closes it. A "NO DATA" message goes out once an alert has been in No data for 5 minutes, with one reminder after 24 hours.

Rules whose expression can't be split into a value and a threshold (for example `a and b`, or `absent(...)`) resolve when their result is empty and the device has polled again; the rule list warns about them.

## Metric history

Prometheus keeps 30 days of history or 10 GB on disk, whichever limit is reached first. When the size cap is hit, the oldest data goes first, so with many devices you may get less than 30 days. Change the limits with `VANTAGE_PROMETHEUS_RETENTION` (default `30d`) and `VANTAGE_PROMETHEUS_RETENTION_SIZE` (default `10GB`) in `.env`, then `docker compose up -d`.

## Time

Vantage takes the time from the host's clock and never sets it; keep the host in sync with its own NTP (chrony or systemd-timesyncd). Admins pick one timezone under **Settings → Time**, and every page, Telegram message and log line uses it. See [ARCHITECTURE.md](ARCHITECTURE.md#time-and-ntp).

## Logs

The backend logs at INFO by default (for example each delivered alert message). Change it with `VANTAGE_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` or `ERROR`) in `.env`. Logs never contain secret values.
