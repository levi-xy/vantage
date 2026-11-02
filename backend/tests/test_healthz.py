from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox
from app.main import create_app


def test_healthz_returns_ok(engine: Engine, db_url: str, secret_box: SecretBox) -> None:
    """Reasons for 503: tests/test_selfcheck.py."""
    settings = Settings(database_url=normalize_database_url(db_url))
    client = TestClient(create_app(settings, secret_box=secret_box))
    for path in ("/healthz", "/healthz/live"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_api_docs_are_disabled_by_default(secret_box: SecretBox) -> None:
    client = TestClient(create_app(Settings(), secret_box=secret_box))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_api_docs_can_be_enabled(secret_box: SecretBox) -> None:
    client = TestClient(create_app(Settings(api_docs=True), secret_box=secret_box))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200
