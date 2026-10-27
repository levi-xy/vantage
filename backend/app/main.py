"""FastAPI application entry point."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api import api_router
from app.api.auth import LOGIN_LIMITS
from app.api.deps import SAFE_METHODS
from app.core.auth import LoginRateLimiter, Passwords
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox, load_master_key
from app.core.db import make_engine, make_session_factory
from app.integrations.registry import Registry, builtin_registry


def create_app(
    settings: Settings | None = None,
    *,
    secret_box: SecretBox | None = None,
    passwords: Passwords | None = None,
    registry: Registry | None = None,
) -> FastAPI:
    """Build the app. Run with ``uvicorn --factory app.main:create_app``.

    Refuses to start (raises ``MasterKeyError``) when no valid master key is configured.
    """
    settings = settings or get_settings()
    secret_box = secret_box or SecretBox(load_master_key())
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
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
    app.state.login_limiter = LoginRateLimiter(LOGIN_LIMITS)
    # Tests pass their own registry; production always uses the built-in list.
    app.state.registry = registry if registry is not None else builtin_registry()

    app.include_router(api_router)
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
