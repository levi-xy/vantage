# Running Vantage

How to run, expose, upgrade and back up a Vantage install, and how its device and alert timing works. Start with the [Quick start](../README.md#quick-start); security details are in [SECURITY.md](SECURITY.md).

## Reaching the dashboard from other machines

The dashboard listens on every address of the server, so it's reachable from your LAN at `https://<server-address>:8443`; `setup.sh` prints the address and puts it in the generated certificate. To listen on one network only, set `VANTAGE_BIND_ADDRESS` in `.env` (in the folder you ran `setup.sh` from) to the server's address on it, or to `127.0.0.1` for the server alone (behind a reverse proxy on the host), then run `docker compose up -d`. Add any other names you use for the server to `VANTAGE_TLS_NAMES` before the first run, so the generated certificate covers them. Give the server a fixed address (a DHCP reservation): the certificate holds the address it had at setup.

An install from before 0.1.0-rc.4 has `VANTAGE_BIND_ADDRESS=127.0.0.1` in its `.env` and is only reachable from the server itself. To open it to the LAN, set `VANTAGE_BIND_ADDRESS=0.0.0.0`, delete `certs/tls.crt` and `certs/tls.key`, and run `./setup.sh` again (it keeps the key, `.env` and data).

Never publish the dashboard to the internet. Recommended: a dedicated monitoring VLAN, outbound internet blocked except the cloud API hosts you use, and dashboard access only from admin machines or your VPN.

## Standalone install

For Portainer, Dockge and other stack managers: a single compose file that needs no files on the host. It is shown in the [README](../README.md#portainer--dockge) and attached to every release as `compose.yml`. `setup.sh` stays the other way to install; the two don't share files, so pick one per server.

On every start a one-shot `init` container (the backend image, no network, read-only, only allowed to change file owners and modes) creates whatever is missing, each in its own named volume: the master key (`master-key`), the Prometheus token (`prometheus-token`), a self-signed certificate (`tls`) and `prometheus.yml` (`prometheus-config`). It gives each file the owner and mode `setup.sh` would and never replaces a file that exists. The other containers start only after it has finished, and mount these volumes read-only, each only the ones it needs.

### Install

- **Portainer:** Stacks → Add stack → Web editor. Paste the file, add any settings under Environment variables, then Deploy the stack.
- **Dockge:** Compose → paste the file into the editor, give the stack a name, add settings in the `.env` box, then Deploy.
- **Command line:** in an empty folder, `curl -fLO https://github.com/levi-xy/vantage/releases/latest/download/compose.yml`, then `docker compose up -d`.

Settings are the same variables as in [`.env.example`](../deploy/.env.example) (port, bind address, HTTPS, retention), set as the stack's environment variables. Before the first start, set `VANTAGE_TLS_NAMES` to the server's address and any names you use for it (for example `192.0.2.10,vantage.home.example`), so the generated certificate covers them. Without it, the certificate covers only `localhost`: browsers warn about the self-signed certificate either way, so this is only about the name in the warning. To regenerate it later, delete the `tls` volume (stop the stack first) and start again.

Open `https://<server-address>:8443` and accept the certificate warning. If the stack doesn't start, read the `init` container's log: it says what is wrong. A TLS volume with only one of the two files (your own certificate without its key, or a first start that was cut off) needs the missing file copied in, or the `tls` volume deleted so a new self-signed pair is made.

With an external database (`VANTAGE_DATABASE_URL` pointing at Postgres), `init` can't tell whether it already holds data, so the first start needs `VANTAGE_FORCE_NEW_KEY=true` to create the key; remove the variable afterwards.

### Keep the volumes

Everything lives in the stack's volumes: the master key, the token, the certificate, the database and the metric history.

- **`docker compose down` is safe.** `docker compose down -v`, `docker volume prune -a` (a plain `prune` on Docker older than 23), or removing the stack together with its volumes (Portainer's and Dockge's delete options) deletes everything, including the master key. Back the key up first (below).
- **The stack's name is part of the volume names** (`<stack>_master-key`, `<stack>_backend-data` and so on). Redeploying under a different stack name starts an empty install; your old data stays in the old volumes. To get it back, deploy under the old name again.

### The setup code (standalone)

The standalone install never prints the setup code, so it doesn't end up in container logs, which stack managers keep and show. Get one from the backend container's console:

- **Portainer:** Containers → `<stack>-backend-1` → Console → Connect (command `/bin/sh`), then run `python -m app.cli setup-code`.
- **Dockge:** the backend's terminal button (`sh`), then `python -m app.cli setup-code`.
- **Command line:** `docker compose exec backend python -m app.cli setup-code`.

### Back up the master key

Do this right after the first start. The key is in the `master-key` volume, readable only by the backend container. Copy it out on the server, from the folder that holds the compose file (Dockge keeps it in `/opt/stacks/<stack>`):

```bash
docker compose cp backend:/run/vantage/key/master_key ./vantage-master.key
```

For a Portainer stack, use the container's name: `docker cp <stack>-backend-1:/run/vantage/key/master_key ./vantage-master.key`. Store the copy somewhere off the server, then delete it there. Without the key, stored device credentials can't be decrypted, and it isn't part of Vantage's own backups.

To put a key back (a new server, or a lost `master-key` volume), stop the stack and copy it into the `init` container, which gives it the right owner and mode on the next start:

```bash
docker compose stop                     # on a new server: docker compose create
docker compose cp ./vantage-master.key init:/vantage-init/key/master_key
docker compose up -d
```

If the key volume is gone but data from an earlier install is still there (the token, the data volume, or an external database), `init` refuses to make a new key and the stack doesn't start. Put the old key back as above, or, if that data is gone or you want to start over, set `VANTAGE_FORCE_NEW_KEY=true` for one start.

### Upgrading (standalone)

Back up first (the backup command below works the same). Then replace the compose file with the new release's `compose.yml` (or change the three `ghcr.io/levi-xy/vantage-…` image tags to the new version) and redeploy: in Portainer, Update the stack with "Re-pull image" on; in Dockge, Save, then Update; on the command line, `docker compose pull && docker compose up -d`. The volumes, and so the key, token, certificate and data, are kept. Database migrations run by themselves.

`init` never replaces `prometheus.yml`. If a release's notes say the Prometheus config changed, stop the stack, delete the `prometheus-config` volume (it holds nothing else) and start again. When a release changes the compose network, take the stack down and up again, as for the setup.sh install.

## The setup code

Creating the first admin in the browser needs a one-time setup code. `setup.sh` prints one after it starts Vantage (the [standalone install](#the-setup-code-standalone) never prints it). If you lost it, it didn't print, or it was already used without creating an admin, get a new one (this replaces the old code) from the folder you ran `setup.sh` in:

```bash
docker compose exec backend python -m app.cli setup-code
```

Once an admin exists the command only says setup is done. Details: [SECURITY.md](SECURITY.md#first-run-setup).

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

This runs while Vantage is up. The master key (`secrets/master.key`, or the `master-key` volume for the [standalone install](#back-up-the-master-key)) is not in the backup: keep its own copy somewhere off the server. Without it, stored device credentials can't be decrypted.

Restoring (`app.cli restore`) and master-key rotation (`app.cli rotate-master-key`, which re-encrypts every stored secret with a new key) are offline. Stop the backend first (`docker compose stop backend`) and run them with `docker compose run --rm -T backend python -m app.cli ...`. They refuse while the backend runs, and the backend doesn't start while one of them runs. On the standalone install, after a rotation put the new key in place the way a key is [put back](#back-up-the-master-key) (copy it into the `init` container), instead of replacing `secrets/master.key`. Details: [SECURITY.md](SECURITY.md#backup-restore-and-master-key-rotation).

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
