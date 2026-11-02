"""FastAPI application entry point."""

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TextIO

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.alerts.channels import default_client_factory
from app.alerts.delivery import DeliveryCollector, DeliveryDispatcher
from app.alerts.evaluator import AlertEvaluator
from app.alerts.selfcheck import SelfMonitor, Watchdog
from app.api import api_router
from app.api.auth import LOGIN_LIMITS, LOGIN_WINDOW_SECONDS
from app.api.deps import SAFE_METHODS
from app.collectors.endpoint import router as metrics_router
from app.collectors.hub import EventHub
from app.collectors.scheduler import PollScheduler
from app.collectors.seed import seed_last_success
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
from app.live.rates import LiveRates
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
    configure_logging(settings.log_level)
    secret_box = secret_box or SecretBox(load_master_key())
    # None when not configured: the Prometheus-only endpoints then answer 503.
    prometheus_token = prometheus_token or load_prometheus_token()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        poller: PollScheduler = app.state.poller
        live_rates: LiveRates = app.state.live_rates
        poller.start()
        # last_success_at from Prometheus, in the background: never delays polling.
        seeding = asyncio.create_task(
            seed_last_success(
                app.state.prometheus, app.state.metrics_store, settings.prometheus_retention
            ),
            name="vantage-seed-last-success",
        )
        live_rates.start()
        evaluator: AlertEvaluator = app.state.alert_evaluator
        evaluator.start()
        watchdog: Watchdog = app.state.alert_watchdog
        watchdog.start()
        dispatcher: DeliveryDispatcher = app.state.delivery
        dispatcher.start()
        try:
            yield
        finally:
            seeding.cancel()
            await asyncio.gather(seeding, return_exceptions=True)
            await watchdog.stop()
            await evaluator.stop()
            await dispatcher.stop()
            await live_rates.stop()
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
    app.state.metrics_store = MetricsStore(
        down_after_failed_cycles=settings.down_after_failed_cycles
    )
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
    # Live traffic rates of the throughput card's ports, pushed on /ws while browsers are open.
    app.state.live_rates = LiveRates(
        session_factory=app.state.session_factory,
        registry=app.state.registry,
        secret_box=secret_box,
        net_guard_factory=app.state.net_guard_factory,
        store=app.state.metrics_store,
        hub=app.state.event_hub,
    )

    # Alerts: the evaluator queues firing/resolved messages in the alert_notifications
    # outbox; the dispatcher delivers them to the rule's channels (Telegram), P0-5.
    # Self-monitoring (P0-4): system alerts, the evaluator's watchdog and /healthz.
    app.state.self_monitor = SelfMonitor(
        session_factory=app.state.session_factory, hub=app.state.event_hub
    )
    app.state.alert_evaluator = AlertEvaluator(
        session_factory=app.state.session_factory,
        prometheus=lambda: app.state.prometheus,
        hub=app.state.event_hub,
        poll_interval=app.state.poller.interval_for_type,
        instance_state=_instance_state(app.state.metrics_store),
        monitor=app.state.self_monitor,
    )
    app.state.alert_watchdog = Watchdog(app.state.self_monitor, app.state.alert_evaluator.restart)
    app.state.delivery = DeliveryDispatcher(
        session_factory=app.state.session_factory,
        secret_box=secret_box,
        client_factory=default_client_factory,
    )
    app.state.metrics_registry.register(DeliveryCollector(app.state.delivery))

    app.include_router(api_router)
    app.include_router(sd_router)
    app.include_router(metrics_router)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.middleware("http")(_api_guard)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """For an external monitor (P0-4): 200, or 503 with one fixed reason code and
        nothing else (``app.alerts.selfcheck.HEALTH_PRIORITY``). Unauthenticated."""
        monitor: SelfMonitor = app.state.self_monitor
        reason = monitor.health()
        if reason is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"reason": reason.value}, status_code=503)

    @app.get("/healthz/live", include_in_schema=False)
    async def healthz_live() -> dict[str, str]:
        """Liveness only: 200 while the web server runs (setup.sh, container checks), so
        an outage elsewhere never makes Docker restart the backend."""
        return {"status": "ok"}

    return app


def _instance_state(store: MetricsStore) -> Callable[[uuid.UUID], str | None]:
    def state(instance_id: uuid.UUID) -> str | None:
        status = store.status(instance_id)
        return status.state if status is not None else None

    return state


def configure_logging(level: str) -> None:
    """Send the ``app`` loggers' records at ``level`` and above to stderr (once).

    Uvicorn configures only its own loggers, so without this only warnings reached the
    output (Python's last-resort handler). Log lines carry ids, counts and fixed codes;
    no code logs a secret.
    """
    logger = logging.getLogger("app")
    logger.setLevel(level)
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stderr`` is at the time of the record."""

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass


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
