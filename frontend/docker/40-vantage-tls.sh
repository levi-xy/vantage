#!/bin/sh
# Writes the listen and Strict-Transport-Security snippets that nginx.conf includes
# (into /tmp, the container's only writable path).
#
# VANTAGE_HTTPS=true (default): HTTPS on 8443 with the settings in tls.conf.
# VANTAGE_HTTPS=false: explicit opt-out, plain HTTP on 8443 (for a LAN without TLS, or
#   behind your own TLS proxy). Session cookies then need VANTAGE_COOKIE_SECURE=false.
# VANTAGE_HSTS=true: send HSTS. Off by default: once a browser has seen HSTS it refuses
#   to open the dashboard while the certificate isn't trusted, which would lock out a
#   self-signed setup. Refused together with VANTAGE_HTTPS=false.
# Any other value stops the container from starting.
set -eu
dir=/tmp/vantage-nginx
mkdir -p "$dir"
fail() { echo "$0: $*" >&2; exit 1; }

case "${VANTAGE_HTTPS:-true}" in
    true) printf 'listen 8443 ssl;\ninclude /etc/nginx/vantage/tls.conf;\n' > "$dir/listen.conf" ;;
    false) echo 'listen 8443;' > "$dir/listen.conf"
           echo "$0: VANTAGE_HTTPS=false, serving plain HTTP (no TLS)" >&2 ;;
    *) fail "VANTAGE_HTTPS must be true or false" ;;
esac

case "${VANTAGE_HSTS:-false}" in
    true)
        [ "${VANTAGE_HTTPS:-true}" = true ] || fail "VANTAGE_HSTS=true needs VANTAGE_HTTPS=true"
        echo 'add_header Strict-Transport-Security "max-age=31536000" always;' > "$dir/hsts.conf" ;;
    false) : > "$dir/hsts.conf" ;;
    *) fail "VANTAGE_HSTS must be true or false" ;;
esac
