#!/bin/sh
# Starts the standalone compose stack from scratch and checks it (CI job "standalone").
#
#   scripts/standalone_smoke.sh dist/compose.yml
#
# The images the file names must already be present locally (CI builds them from the
# checkout and writes their tag into the file with deploy/make_bundle.py). Removes the
# stack and its volumes at the end. Needs docker, curl and sha256sum.
set -eu

file=${1:?usage: standalone_smoke.sh <compose file>}
dir=$(mktemp -d)
cp "$file" "$dir/compose.yml"
cd "$dir"
dc() { docker compose -p vantage-smoke -f compose.yml "$@"; }

fail() {
    echo "FAIL: $*" >&2
    dc ps -a >&2 || true
    dc logs --no-color >&2 || true
    exit 1
}
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$dir"; }
trap cleanup EXIT

dc pull -q prometheus
# up -d returns once init has finished; it fails if init fails.
dc up -d --pull never || fail "the stack didn't start"

echo "Waiting for /healthz/live over HTTPS"
i=0
until curl -fsk -o /dev/null https://127.0.0.1:8443/healthz/live; do
    i=$((i + 1))
    [ "$i" -lt 90 ] || fail "/healthz/live didn't answer 200"
    sleep 2
done
echo "ok: /healthz/live"

# Owners and modes as setup.sh sets them (as root): uid gid mode per file.
check_stat() {  # $1 = service, $2 = path, $3 = expected "mode uid gid"
    got=$(dc exec -T "$1" stat -c '%a %u %g' "$2" | tr -d '\r')
    [ "$got" = "$3" ] || fail "$2 in $1 is '$got', want '$3'"
}
check_stat backend /run/vantage/key/master_key "400 10001 0"
check_stat backend /run/secrets/prometheus_token "440 10001 65534"
check_stat frontend /run/secrets/tls_key "400 101 0"
check_stat frontend /run/secrets/tls_cert "644 0 0"
check_stat prometheus /etc/vantage/prometheus.yml "644 0 0"
echo "ok: owners and modes"

# The ping sysctl the compose file sets is what the backend's network namespace has.
want=$(sed -n 's/.*net\.ipv4\.ping_group_range: *"\([0-9 ]*\)".*/\1/p' compose.yml | head -n 1)
[ -n "$want" ] || fail "no ping_group_range in the compose file"
got=$(dc exec -T backend cat /proc/sys/net/ipv4/ping_group_range | tr -d '\r' | tr -s '[:space:]' ' ' | sed 's/ $//')
[ "$got" = "$want" ] || fail "ping_group_range in backend is '$got', want '$want'"
echo "ok: ping_group_range is $got"

# The volumes are read-only for the services.
if dc exec -T backend sh -c ': > /run/vantage/key/x' 2>/dev/null; then
    fail "the backend can write to the key volume"
fi

# Prometheus reads the token and scrapes the backend.
echo "Waiting for Prometheus to scrape the backend"
query='up%7Bjob%3D%22vantage-backend%22%7D'
i=0
until dc exec -T prometheus wget -qO- "http://localhost:9090/api/v1/query?query=$query" \
    2>/dev/null | grep -q ',"1"\]'; do
    i=$((i + 1))
    [ "$i" -lt 60 ] || fail "Prometheus doesn't scrape the backend"
    sleep 3
done
echo "ok: Prometheus scrapes the backend"

# The setup code comes from the console and never shows in the logs.
code=$(dc exec -T backend python -m app.cli setup-code --quiet | tail -n 1 | tr -d '\r')
case "$code" in
    ????-????-????-????) ;;
    *) fail "no setup code from the console" ;;
esac
if dc logs --no-color | grep -qF "$code"; then
    fail "the setup code is in the logs"
fi
echo "ok: setup code from the console, not in the logs"

# The documented key backup, and a restart that keeps every file.
dc cp backend:/run/vantage/key/master_key ./master.key
key_sum=$(sha256sum < master.key | cut -d' ' -f1)
in_container=$(dc exec -T backend sha256sum /run/vantage/key/master_key | cut -d' ' -f1)
[ "$key_sum" = "$in_container" ] || fail "docker compose cp didn't copy the key"
cert_sum=$(dc exec -T frontend sha256sum /run/secrets/tls_cert | tr -d '\r')
dc up -d --pull never --force-recreate || fail "the restart failed"
dc logs --no-color init | grep -q "Keeping the existing master key" \
    || fail "init didn't keep the master key"
[ "$(dc exec -T frontend sha256sum /run/secrets/tls_cert | tr -d '\r')" = "$cert_sum" ] \
    || fail "the certificate changed on restart"
dc cp backend:/run/vantage/key/master_key ./master.key.after
[ "$(sha256sum < master.key.after | cut -d' ' -f1)" = "$key_sum" ] \
    || fail "the master key changed on restart"
echo "ok: key backup and restart keep the files"

echo "standalone stack: all checks passed"
