"""FastAPI application entry point."""

from fastapi import FastAPI

from app import __version__


def create_app() -> FastAPI:
    # Interactive API docs stay off until config (M1.1) can enable them for dev only.
    app = FastAPI(
        title="Vantage",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
