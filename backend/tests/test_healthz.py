from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.main import create_app


def test_healthz_returns_ok(secret_box: SecretBox) -> None:
    client = TestClient(create_app(Settings(), secret_box=secret_box))
    response = client.get("/healthz")
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
