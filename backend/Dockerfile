FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install ".[postgres]" && useradd --system --uid 10001 --no-create-home vantage \
    && mkdir /data && chown vantage /data

USER vantage
EXPOSE 8000
# Apply database migrations, then start the API. --no-proxy-headers keeps the request's
# client address the real TCP peer: the app decides itself which proxies to trust
# (VANTAGE_TRUSTED_PROXIES), so uvicorn must not rewrite it from X-Forwarded-For.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
