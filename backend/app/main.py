"""FastAPI application entry point."""

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TextIO

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.alerts.channels import default_client_factory
from app.alerts.delivery import DeliveryCollector, DeliveryDispatcher
from app.alerts.evaluator import AlertEvaluator
from app.alerts.key_monitor import KeyMonitor
from app.alerts.selfcheck import SelfMonitor, Watchdog
from app.api import api_router
from app.api.auth import LOGIN_LIMITS, LOGIN_WINDOW_SECONDS
from app.api.deps import SAFE_METHODS
from app.collectors.endpoint import router as metrics_router
from app.collectors.hub import EventHub
from app.collectors.scheduler import PollScheduler
from app.collectors.seed import seed_last_success
from app.collectors.store import MetricsStore, make_registry
from app.core import timezone
from app.core.auth import LoginRateLimiter, Passwords
from app.core.config import Settings, get_settings, host_allowed
from app.core.crypto import SecretBox, load_master_key
from app.core.db import make_engine, make_session_factory
from app.core.key_check import KeyMismatchError
from app.core.login_failures import UserLoginFailures
from app.core.maintenance import HEARTBEAT_SECONDS
from app.core.net_guard import NetGuard, NetPolicy
from app.core.prometheus_auth import PrometheusToken, load_prometheus_token
from app.heartbeat import Heartbeat
from app.integrations.instance_config import NetGuardFactory, default_net_guard_factory
from app.integrations.registry import Registry, builtin_registry
from app.live.rates import LiveRates
from app.query.proxy import PrometheusClient, QueryGate
from app.sd.targets import router as sd_router

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    secret_box: SecretBox | None = None,
    passwords: Passwords | None = None,
    registry: Registry | None = None,
    net_guard_factory: NetGuardFactory | None = None,
    prometheus_token: PrometheusToken | None = None,
    prometheus_client: PrometheusClient | None = None,
    alert_prometheus_client: PrometheusClient | None = None,
) -> FastAPI:
    """Build the app. Run with ``uvicorn --factory app.main:create_app``.

    Refuses to start (raises ``MasterKeyError``) when no valid master key is configured,
    or (``PrometheusTokenError``) when a Prometheus token is configured but unusable.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    # httpx logs every request URL at INFO, and the Telegram API takes the bot token in
    # the URL path: keep both client libraries at WARNING whatever VANTAGE_LOG_LEVEL is.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    secret_box = secret_box or SecretBox(load_master_key())
    # None when not configured: the Prometheus-only endpoints then answer 503.
    prometheus_token = prometheus_token or load_prometheus_token()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The heartbeat and the maintenance lock (P0-6) come first: with a lock nothing
        # starts until it is gone. Then the master key check (P0-6): in key_mismatch mode
        # polling, live rates and delivery don't start (the key monitor starts them once
        # it matches).
        heartbeat: Heartbeat = app.state.heartbeat
        try:
            with app.state.session_factory() as db:
                timezone.load(db)
        except Exception as exc:  # stays UTC until an admin saves a zone
            log.warning("timezone load failed (%s)", type(exc).__name__)
        try:
            await heartbeat.beat()
        except Exception as exc:  # retried on every beat
            log.warning("heartbeat at startup failed (%s)", type(exc).__name__)
        key_monitor: KeyMonitor = app.state.key_monitor
        try:
            await key_monitor.check()
        except Exception as exc:  # re-checked every few seconds; secret writes check too
            log.warning("master key check at startup failed (%s)", type(exc).__name__)
        await workers.apply()
        # last_success_at from Prometheus, in the background: never delays polling.
        seeding = asyncio.create_task(
            seed_last_success(
                app.state.prometheus, app.state.metrics_store, settings.prometheus_retention
            ),
            name="vantage-seed-last-success",
        )
        heartbeat.start()
        key_monitor.start()
        try:
            yield
        finally:
            seeding.cancel()
            await asyncio.gather(seeding, return_exceptions=True)
            await heartbeat.stop()
            await key_monitor.stop()
            await workers.stop()
            await app.state.prometheus.aclose()
            await app.state.alert_prometheus.aclose()
            # Last: the CLI may run as soon as the heartbeat is gone.
            await heartbeat.shutdown()
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
    # Browser queries run at most a few at a time (in total and per session); a full
    # slot answers 429 instead of queueing.
    app.state.query_gate = QueryGate()
    # The alert evaluator gets its own client (own connection pool, no /query caps), so
    # browser queries can never starve alert evaluation. Tests may inject a fake one.
    app.state.alert_prometheus = alert_prometheus_client or PrometheusClient.guarded(
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
        prometheus=lambda: app.state.alert_prometheus,
        hub=app.state.event_hub,
        poll_interval=app.state.poller.interval_for_type,
        instance_state=_instance_state(app.state.metrics_store),
        failed_cycles=app.state.metrics_store.failed_cycles,
        monitor=app.state.self_monitor,
    )
    app.state.alert_watchdog = Watchdog(app.state.self_monitor, app.state.alert_evaluator.restart)
    app.state.delivery = DeliveryDispatcher(
        session_factory=app.state.session_factory,
        secret_box=secret_box,
        client_factory=default_client_factory,
    )
    app.state.metrics_registry.register(DeliveryCollector(app.state.delivery))

    workers = _Workers(
        poller=app.state.poller,
        live_rates=app.state.live_rates,
        delivery=app.state.delivery,
        evaluator=app.state.alert_evaluator,
        watchdog=app.state.alert_watchdog,
    )
    app.state.workers = workers

    async def pause_secret_users() -> None:
        workers.key_mismatch = True
        await workers.apply()

    async def resume_secret_users() -> None:
        workers.key_mismatch = False
        await workers.apply()

    app.state.key_monitor = KeyMonitor(
        session_factory=app.state.session_factory,
        secret_box=secret_box,
        monitor=app.state.self_monitor,
        pause=pause_secret_users,
        resume=resume_secret_users,
        # No key check writes (check value, system alert, outbox) in maintenance mode.
        suspended=lambda: workers.maintenance,
    )

    async def maintenance_changed(active: bool) -> None:
        app.state.self_monitor.maintenance = active
        workers.maintenance = active
        if not active:
            # The offline command may have changed the key or the secrets: check before
            # anything that decrypts them resumes.
            try:
                await app.state.key_monitor.check()
            except Exception as exc:  # re-checked every few seconds
                log.warning("master key check after maintenance failed (%s)", type(exc).__name__)
        await workers.apply()

    app.state.heartbeat = Heartbeat(engine, maintenance_changed, interval=HEARTBEAT_SECONDS)

    app.include_router(api_router)
    app.include_router(sd_router)
    app.include_router(metrics_router)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(KeyMismatchError, _key_mismatch)
    app.middleware("http")(_api_guard)
    if settings.public_hosts:
        # Added last, so it runs first, for HTTP and WebSocket requests alike.
        app.add_middleware(PublicHostGuard, allowed=settings.public_hosts)

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


class _Workers:
    """The background work that pauses: everything that decrypts secrets (polling, live
    rates, delivery) in key_mismatch mode, and that plus alert evaluation (with its
    watchdog, which would otherwise restart it) in maintenance mode."""

    def __init__(
        self,
        *,
        poller: PollScheduler,
        live_rates: LiveRates,
        delivery: DeliveryDispatcher,
        evaluator: AlertEvaluator,
        watchdog: Watchdog,
    ) -> None:
        self._poller = poller
        self._live_rates = live_rates
        self._delivery = delivery
        self._evaluator = evaluator
        self._watchdog = watchdog
        self.key_mismatch = False
        self.maintenance = False
        self._secret_users = False
        self._evaluating = False
        self._lock = asyncio.Lock()

    async def apply(self) -> None:
        """Start or stop each group to match the current modes."""
        async with self._lock:
            want_evaluating = not self.maintenance
            want_secret_users = not (self.maintenance or self.key_mismatch)
            if self._evaluating and not want_evaluating:
                await self._watchdog.stop()
                await self._evaluator.stop()
                self._evaluating = False
            if self._secret_users and not want_secret_users:
                await self._poller.stop()
                await self._live_rates.stop()
                await self._delivery.stop()
                self._secret_users = False
            if want_secret_users and not self._secret_users:
                self._poller.start()
                self._live_rates.start()
                self._delivery.start()
                self._secret_users = True
            if want_evaluating and not self._evaluating:
                self._evaluator.start()
                self._watchdog.start()
                self._evaluating = True

    async def stop(self) -> None:
        async with self._lock:
            await self._watchdog.stop()
            await self._evaluator.stop()
            await self._delivery.stop()
            await self._live_rates.stop()
            await self._poller.stop()
            self._secret_users = self._evaluating = False


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
        handler.setFormatter(_ZoneFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


class _ZoneFormatter(logging.Formatter):
    """Timestamps in the configured timezone, with offset (maintainer-approved 2026-11-07)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return timezone.stamp(datetime.fromtimestamp(record.created, UTC))


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


async def _key_mismatch(_request: Request, _exc: Exception) -> Response:
    """A secret write refused in key_mismatch mode (P0-6); nothing was written."""
    return JSONResponse(
        {
            "detail": "the master key doesn't match the stored secrets; fix the key file "
            "and restart the backend",
            "reason": "key_mismatch",
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


# State-changing requests still served in maintenance mode: logging in and out, and the
# PromQL proxy (a read sent as POST).
MAINTENANCE_ALLOWED = frozenset({"/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/query"})


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
    heartbeat: Heartbeat | None = getattr(request.app.state, "heartbeat", None)
    if (
        heartbeat is not None
        and heartbeat.maintenance
        and request.method not in SAFE_METHODS
        and request.url.path.rstrip("/") not in MAINTENANCE_ALLOWED
    ):
        # P0-6: an offline command (restore, key rotation) holds the maintenance lock.
        return JSONResponse(
            {
                "detail": "maintenance in progress: changes are refused until it finishes",
                "reason": "maintenance",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "no-store"},
        )
    response = await call_next(request)
    # API responses carry user data and CSRF tokens; keep them out of caches.
    response.headers["Cache-Control"] = "no-store"
    return response


class PublicHostGuard:
    """Refuse ``/api/*`` (including the ``/api/v1/ws`` socket) for a Host not listed in
    ``VANTAGE_PUBLIC_HOSTS``: 400 for HTTP, a refused handshake for WebSocket.

    A DNS-rebinding page reaches the backend under its own host name; this stops it from
    using the API. ``/healthz``, ``/sd/targets`` and ``/metrics`` stay reachable by their
    compose-network names.
    """

    def __init__(self, app: ASGIApp, allowed: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not str(scope["path"]).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        host = ""
        for key, value in scope["headers"]:
            if key == b"host":
                host = value.decode("latin-1")
                break
        if host_allowed(host, self.allowed):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})  # handshake answers 403
            return
        response = JSONResponse({"detail": "unknown host"}, status_code=400)
        await response(scope, receive, send)
