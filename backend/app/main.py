"""FastAPI application entry point."""

from fastapi import FastAPI

from app import __version__
from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
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

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
