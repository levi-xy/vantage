FROM python:3.12-slim

LABEL org.opencontainers.image.title="Vantage backend" \
      org.opencontainers.image.description="Vantage API, pollers and alerting" \
      org.opencontainers.image.url="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.source="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
# The license and notice (the repository root, passed as the "license" build context).
COPY --from=license LICENSE NOTICE /usr/share/doc/vantage/
# The user and its group have fixed IDs (10001). The group must stay inside the compose
# files' net.ipv4.ping_group_range ("0 65535"; tests/test_compose_network.py checks it),
# or the backend can't open unprivileged ICMP sockets for ping checks.
RUN pip install ".[postgres]" && groupadd --system --gid 10001 vantage \
    && useradd --system --uid 10001 --gid 10001 --no-create-home vantage \
    && mkdir /data && chown vantage /data

USER vantage
EXPOSE 8000
# First the startup gate (P0-6): it exits 1, so nothing is migrated or started, while an
# offline command (restore, master-key rotation) holds the maintenance lock; the restart
# policy (compose: unless-stopped) retries until the lock is gone. Then apply database
# migrations and start the API. --no-proxy-headers keeps the request's client address the
# real TCP peer: the app decides itself which proxies to trust (VANTAGE_TRUSTED_PROXIES),
# so uvicorn must not rewrite it from X-Forwarded-For.
CMD ["sh", "-c", "python -m app.cli startup-gate && alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
