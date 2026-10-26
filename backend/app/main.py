"""FastAPI application entry point."""

from fastapi import FastAPI

from app import __version__
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox, load_master_key


def create_app(settings: Settings | None = None, *, secret_box: SecretBox | None = None) -> FastAPI:
    """Build the app. Run with ``uvicorn --factory app.main:create_app``.

    Refuses to start (raises ``MasterKeyError``) when no valid master key is configured.
    """
    settings = settings or get_settings()
    secret_box = secret_box or SecretBox(load_master_key())
    # Interactive API docs are off unless VANTAGE_API_DOCS is set (development only).
    docs = settings.api_docs
    app = FastAPI(
        title="Vantage",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.secret_box = secret_box

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
