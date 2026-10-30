#!/bin/sh
# Vantage one-stop setup: creates the missing secrets and .env, then starts the stack.
#
#   ./setup.sh                  create what's missing, start, wait until healthy
#   ./setup.sh --no-start       only create the files
#   ./setup.sh --force-new-key  allow a new master key even though data may exist
#
# Safe to re-run: existing files are kept (the master key is never overwritten),
# and the secret file permissions are fixed on every run. No secret is ever printed.
set -eu

BACKEND_UID=10001     # backend container user (backend/Dockerfile)
PROMETHEUS_GID=65534  # prometheus container user/group (nobody)
VOLUME=vantage_backend-data

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

start=1
force_new_key=0
for arg in "$@"; do
    case "$arg" in
        --no-start) start=0 ;;
        --force-new-key) force_new_key=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "Error: $*" >&2; exit 1; }

cd "$(dirname "$0")"
[ -f docker-compose.yml ] && [ -f .env.example ] || die "run this from the Vantage deploy/ directory"
command -v openssl >/dev/null 2>&1 || die "openssl is required"

umask 077
mkdir -p secrets
# Only the owner can enter secrets/, so other host users can't read the files in it
# whatever their own mode is. Containers still can: Docker mounts the files directly.
chmod 700 secrets

# Value of a KEY=value line in .env (last one wins), without sourcing the file.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | sed 's/^["'\'']//; s/["'\'']$//'
}

# Signs that an install already stored data encrypted with an earlier key.
data_may_exist() {
    [ -f secrets/prometheus_token ] && return 0
    if command -v docker >/dev/null 2>&1 && docker volume inspect "$VOLUME" >/dev/null 2>&1; then
        return 0
    fi
    case "$(env_value VANTAGE_DATABASE_URL)" in
        ""|sqlite*) return 1 ;;
        *) return 0 ;;  # external database (Postgres): can't tell, assume it has data
    esac
}

# Write a new secret through a temporary file, so a failure never leaves a partial one.
new_secret() {  # $1 = target file, rest = openssl rand arguments
    target=$1; shift
    tmp="$target.tmp.$$"
    if ! openssl rand "$@" > "$tmp"; then rm -f "$tmp"; die "openssl failed"; fi
    mv "$tmp" "$target"
}

backup_reminder() {
    [ "$key_created" -eq 1 ] || return 0
    echo
    echo "IMPORTANT: back up deploy/secrets/master.key now, somewhere other than this"
    echo "server. Without it, the device credentials Vantage stores can't be decrypted."
}

key_created=0
if [ -f secrets/master.key ]; then
    echo "Keeping existing secrets/master.key"
else
    if data_may_exist && [ "$force_new_key" -eq 0 ]; then
        die "secrets/master.key is missing, but data from an earlier install may exist
(secrets/prometheus_token, the $VOLUME volume, or an external database).
Stored device credentials can only be read with the original key: restore it
from your backup into deploy/secrets/master.key. If that data is gone or you
want to start over, re-run with --force-new-key."
    fi
    new_secret secrets/master.key -base64 32
    key_created=1
    # From here on, remind on every exit, including a failure further down: a re-run
    # keeps the key and wouldn't remind again.
    trap backup_reminder EXIT
    echo "Created secrets/master.key"
fi

if [ -f secrets/prometheus_token ]; then
    echo "Keeping existing secrets/prometheus_token"
else
    new_secret secrets/prometheus_token -hex 32
    echo "Created secrets/prometheus_token"
fi

if [ -f .env ]; then
    echo "Keeping existing .env"
else
    cp .env.example .env
    echo "Created .env from .env.example (edit it to change the address or port)"
fi

if [ "$(id -u)" -eq 0 ]; then
    # Running as root: give each file to the container user that reads it.
    chown "$BACKEND_UID" secrets/master.key
    chmod 400 secrets/master.key
    chown "$BACKEND_UID:$PROMETHEUS_GID" secrets/prometheus_token
    chmod 440 secrets/prometheus_token
else
    # Not root: we can't chown to the container users, so the files must be readable
    # by them through their mode; the 700 secrets/ directory keeps everyone else out.
    chmod 644 secrets/master.key secrets/prometheus_token
fi

if [ "$start" -eq 0 ]; then
    echo "Files ready. Start Vantage with: docker compose up -d"
    exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 ('docker compose') is not available"
docker info >/dev/null 2>&1 || die "can't reach the Docker daemon (is it running, and may this user use it?)"

echo "Starting Vantage (the first run builds the images and takes a few minutes)..."
docker compose up -d

bind=$(env_value VANTAGE_BIND_ADDRESS); bind=${bind:-127.0.0.1}
port=$(env_value VANTAGE_HTTP_PORT); port=${port:-8080}
case "$bind" in
    0.0.0.0|::|"[::]") check_host=127.0.0.1; shown_host="<this-server's-address>" ;;
    *:*) check_host="[$bind]"; shown_host="[$bind]" ;;
    *) check_host=$bind; shown_host=$bind ;;
esac

probe_tool=
if command -v curl >/dev/null 2>&1; then probe_tool=curl
elif command -v wget >/dev/null 2>&1; then probe_tool=wget
fi
probe() {
    if [ "$probe_tool" = curl ]; then
        curl -fsS -o /dev/null --max-time 5 "http://$check_host:$port/healthz" 2>/dev/null
    else
        wget -q -O /dev/null -T 5 "http://$check_host:$port/healthz" 2>/dev/null
    fi
}

healthy=0
if [ -z "$probe_tool" ]; then
    echo "Neither curl nor wget found; not waiting for the health check."
else
    printf "Waiting for Vantage to answer"
    i=0
    while [ "$i" -lt 90 ]; do
        if probe; then healthy=1; break; fi
        printf "."
        sleep 2
        i=$((i + 1))
    done
    echo
fi

if [ "$healthy" -eq 1 ]; then
    echo "Vantage is running: http://$shown_host:$port"
    echo "Open it and create the admin account."
else
    echo "Vantage didn't report healthy yet. Check: docker compose ps; docker compose logs backend"
    echo "Address once it's up: http://$shown_host:$port"
fi
