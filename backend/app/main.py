"""FastAPI application entry point."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api import api_router
from app.api.auth import LOGIN_LIMITS, LOGIN_WINDOW_SECONDS
from app.api.deps import SAFE_METHODS
from app.collectors.endpoint import router as metrics_router
from app.collectors.hub import EventHub
from app.collectors.scheduler import PollScheduler
from app.collectors.store import MetricsStore, make_registry
from app.core.auth import LoginRateLimiter, Passwords
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox, load_master_key
from app.core.db import make_engine, make_session_factory
from app.core.login_failures import UserLoginFailures
from app.core.net_guard import NetGuard, NetPolicy
from app.core.prometheus_auth import PrometheusToken, load_prometheus_token
from app.integrations.instance_config import NetGuardFactory, default_net_guard_factory
from app.integrations.registry import Registry, builtin_registry
from app.query.proxy import PrometheusClient
from app.sd.targets import router as sd_router


def create_app(
    settings: Settings | None = None,
    *,
    secret_box: SecretBox | None = None,
    passwords: Passwords | None = None,
    registry: Registry | None = None,
    net_guard_factory: NetGuardFactory | None = None,
    prometheus_token: PrometheusToken | None = None,
    prometheus_client: PrometheusClient | None = None,
) -> FastAPI:
    """Build the app. Run with ``uvicorn --factory app.main:create_app``.

    Refuses to start (raises ``MasterKeyError``) when no valid master key is configured,
    or (``PrometheusTokenError``) when a Prometheus token is configured but unusable.
    """
    settings = settings or get_settings()
    secret_box = secret_box or SecretBox(load_master_key())
    # None when not configured: the Prometheus-only endpoints then answer 503.
    prometheus_token = prometheus_token or load_prometheus_token()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        poller: PollScheduler = app.state.poller
        poller.start()
        try:
            yield
        finally:
            await poller.stop()
            await app.state.prometheus.aclose()
            engine.dispose()

    # Interactive API docs are off unless VANTAGE_API_DOCS is set (development only).
    docs = settings.api_docs
    app = FastAPI(
        title="Vantage",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.secret_box = secret_box
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.passwords = passwords or Passwords()
    app.state.login_limiter = LoginRateLimiter(LOGIN_LIMITS, window_seconds=LOGIN_WINDOW_SECONDS)
    # Failed logins for existing users: in the database, so they survive a restart (M1.8).
    app.state.login_failures = UserLoginFailures(
        LOGIN_LIMITS["user"], window=timedelta(seconds=LOGIN_WINDOW_SECONDS)
    )
    # Tests pass their own registry; production always uses the built-in list.
    app.state.registry = registry if registry is not None else builtin_registry()
    # Tests inject a guard with a fake resolver; production uses the default policy.
    app.state.net_guard_factory = net_guard_factory or default_net_guard_factory
    app.state.prometheus_token = prometheus_token
    # PromQL proxy (M5.1): the URL comes from server config only, never from a request.
    # Through net_guard with the default policy, like every outbound connection.
    app.state.prometheus = prometheus_client or PrometheusClient.guarded(
        settings.prometheus_url, NetGuard(NetPolicy())
    )
    # Poll collector (M2.4): results kept in memory, served on /metrics.
    app.state.metrics_store = MetricsStore()
    app.state.metrics_registry = make_registry(app.state.metrics_store)
    app.state.event_hub = EventHub()
    app.state.poller = PollScheduler(
        session_factory=app.state.session_factory,
        registry=app.state.registry,
        secret_box=secret_box,
        net_guard_factory=app.state.net_guard_factory,
        store=app.state.metrics_store,
        hub=app.state.event_hub,
    )

    app.include_router(api_router)
    app.include_router(sd_router)
    app.include_router(metrics_router)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.middleware("http")(_api_guard)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _validation_error(_request: Request, exc: Exception) -> Response:
    """Like FastAPI's 422, minus the submitted values: they may be passwords or tokens."""
    if not isinstance(exc, RequestValidationError):  # registered only for this type
        raise exc
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


async def _api_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    # Browsers send Sec-Fetch-Site; refuse writes started by another site outright.
    if request.method not in SAFE_METHODS and request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse(
            {"detail": "cross-site request refused"}, status_code=status.HTTP_403_FORBIDDEN
        )
    response = await call_next(request)
    # API responses carry user data and CSRF tokens; keep them out of caches.
    response.headers["Cache-Control"] = "no-store"
    return response
