#!/bin/sh
# Vantage one-stop setup: creates the missing secrets, HTTPS certificate and .env,
# then starts the stack.
#
#   ./setup.sh                  create what's missing, start, wait until healthy
#   ./setup.sh --no-start       only create the files
#   ./setup.sh --force-new-key  allow a new master key even though data may exist
#
# Safe to re-run: existing files are kept (the master key and certificate are never
# overwritten), and the secret file permissions are fixed on every run. No secret is
# ever printed.
set -eu

BACKEND_UID=10001     # backend container user (backend/Dockerfile)
PROMETHEUS_GID=65534  # prometheus container user/group (nobody)
FRONTEND_UID=101      # frontend container user (nginx-unprivileged image)
VOLUME=vantage_backend-data

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
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

# Names for the self-signed certificate's subjectAltName, as "DNS:x" / "IP:x" entries.
san_entry() {  # $1 = host name or IP address
    case "$1" in
        *[!A-Za-z0-9.:*-]*) die "VANTAGE_TLS_NAMES: '$1' is not a host name or IP address" ;;
        *:*) echo "IP:$1" ;;                  # IPv6
        *[!0-9.]*) echo "DNS:$1" ;;
        *) echo "IP:$1" ;;                    # IPv4
    esac
}

new_self_signed_cert() {
    sans="DNS:localhost,IP:127.0.0.1,IP:::1"
    cn=vantage
    host=$(hostname 2>/dev/null || true)
    case "$host" in
        ""|*[!A-Za-z0-9.-]*) ;;
        *) cn=$host; sans="$sans,DNS:$host" ;;
    esac
    bind=$(env_value VANTAGE_BIND_ADDRESS)
    case "$bind" in
        ""|0.0.0.0|::|"[::]"|127.0.0.1) ;;
        *) bind=${bind#[}; sans="$sans,$(san_entry "${bind%]}")" ;;
    esac
    old_ifs=$IFS; IFS=', '; set -f  # split on commas/spaces, no globbing of "*.x"
    for name in $(env_value VANTAGE_TLS_NAMES); do
        sans="$sans,$(san_entry "$name")"
    done
    IFS=$old_ifs; set +f

    cfg="certs/openssl.cnf.tmp.$$"
    cat > "$cfg" <<EOF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3
[dn]
CN = $cn
O = Vantage self-signed
[v3]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
subjectAltName = $sans
EOF
    # umask 077 (above): the key is created readable by its owner only.
    if ! openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 -config "$cfg" \
        -keyout "certs/tls.key.tmp.$$" -out "certs/tls.crt.tmp.$$" >/dev/null 2>&1; then
        rm -f "$cfg" "certs/tls.key.tmp.$$" "certs/tls.crt.tmp.$$"
        die "openssl couldn't create the certificate"
    fi
    rm -f "$cfg"
    mv "certs/tls.key.tmp.$$" certs/tls.key
    mv "certs/tls.crt.tmp.$$" certs/tls.crt
    echo "Created a self-signed HTTPS certificate in certs/ for: $sans"
}

mkdir -p certs
chmod 700 certs
cert_created=0
if [ -f certs/tls.crt ] && [ -f certs/tls.key ]; then
    echo "Keeping existing certs/tls.crt and certs/tls.key"
elif [ -f certs/tls.crt ] || [ -f certs/tls.key ]; then
    die "only one of certs/tls.crt and certs/tls.key exists. Add the missing one (your
certificate and its key), or remove the other to have a self-signed pair generated."
else
    new_self_signed_cert
    cert_created=1
fi

if [ "$(id -u)" -eq 0 ]; then
    # Running as root: give each file to the container user that reads it.
    chown "$BACKEND_UID" secrets/master.key
    chmod 400 secrets/master.key
    chown "$BACKEND_UID:$PROMETHEUS_GID" secrets/prometheus_token
    chmod 440 secrets/prometheus_token
    chown "$FRONTEND_UID" certs/tls.key
    chmod 400 certs/tls.key
else
    # Not root: we can't chown to the container users, so the files must be readable
    # by them through their mode; the 700 secrets/ and certs/ directories keep everyone
    # else out.
    chmod 644 secrets/master.key secrets/prometheus_token certs/tls.key
fi
chmod 644 certs/tls.crt  # public anyway

# An .env from before HTTPS may still say COOKIE_SECURE=false for plain-HTTP LAN use.
if [ "$(env_value VANTAGE_HTTPS)" != false ] && [ "$(env_value VANTAGE_COOKIE_SECURE)" = false ]; then
    echo "Warning: HTTPS is on but VANTAGE_COOKIE_SECURE=false in .env, so the session cookie"
    echo "can still be sent over plain HTTP. Remove that line (or set it to true)."
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
port=$(env_value VANTAGE_HTTPS_PORT)
[ -n "$port" ] || port=$(env_value VANTAGE_HTTP_PORT)  # the name before HTTPS
port=${port:-8443}
case "$bind" in
    0.0.0.0|::|"[::]") shown_host="<this-server's-address>" ;;
    *:*) shown_host="[${bind#[}"; shown_host="${shown_host%]}]" ;;
    *) shown_host=$bind ;;
esac

# Health check from inside the frontend container, through nginx's upstream to the
# backend: needs no curl/wget on the host, and no TLS verification exceptions for a
# self-signed or not-yet-trusted certificate. /healthz/live (always 200 while the backend
# runs), not /healthz: a 503 for an outage (Prometheus not up yet) must not fail setup.
probe() {
    docker compose exec -T frontend wget -q -O /dev/null -T 5 http://backend:8000/healthz/live \
        >/dev/null 2>&1
}

healthy=0
printf "Waiting for Vantage to answer"
i=0
while [ "$i" -lt 90 ]; do
    if probe; then healthy=1; break; fi
    printf "."
    sleep 2
    i=$((i + 1))
done
echo

scheme=https
[ "$(env_value VANTAGE_HTTPS)" = false ] && scheme=http
url="$scheme://$shown_host:$port"
if [ "$healthy" -eq 1 ]; then
    echo "Vantage is running: $url"
    echo "Open it and create the admin account."
else
    echo "Vantage didn't report healthy yet. Check: docker compose ps; docker compose logs frontend backend"
    echo "Address once it's up: $url"
fi
if [ "$cert_created" -eq 1 ] && [ "$scheme" = https ]; then
    echo
    echo "The certificate is self-signed, so browsers show a warning the first time; check"
    echo "the address and accept it. To use your own instead, replace certs/tls.crt and"
    echo "certs/tls.key, then run ./setup.sh (it fixes the key's owner and mode) and"
    echo "docker compose restart frontend"
fi
