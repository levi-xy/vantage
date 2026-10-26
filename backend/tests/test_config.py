import pytest

from app.core.config import DEFAULT_DATABASE_URL, ConfigError, load_settings


def test_defaults() -> None:
    settings = load_settings({})
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.database_url.startswith("sqlite")
    assert settings.api_docs is False


def test_sqlite_url_is_kept() -> None:
    url = "sqlite:////data/vantage.db"
    assert load_settings({"VANTAGE_DATABASE_URL": url}).database_url == url


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_plain_postgres_url_uses_psycopg3(scheme: str) -> None:
    url = f"{scheme}://vantage:example-password@db.example:5432/vantage"
    settings = load_settings({"VANTAGE_DATABASE_URL": url})
    assert settings.database_url == (
        "postgresql+psycopg://vantage:example-password@db.example:5432/vantage"
    )


def test_explicit_postgres_driver_is_kept() -> None:
    url = "postgresql+psycopg://vantage@db.example/vantage"
    assert load_settings({"VANTAGE_DATABASE_URL": url}).database_url == url


def test_empty_database_url_falls_back_to_default() -> None:
    assert load_settings({"VANTAGE_DATABASE_URL": ""}).database_url == DEFAULT_DATABASE_URL


def test_unsupported_database_url_is_rejected_without_echoing_it() -> None:
    url = "mysql://vantage:example-password@db.example/vantage"
    with pytest.raises(ConfigError) as exc:
        load_settings({"VANTAGE_DATABASE_URL": url})
    assert "example-password" not in str(exc.value)
    assert "VANTAGE_DATABASE_URL" in str(exc.value)


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("off", False)])
def test_api_docs_flag(raw: str, expected: bool) -> None:
    assert load_settings({"VANTAGE_API_DOCS": raw}).api_docs is expected


def test_invalid_bool_is_rejected() -> None:
    with pytest.raises(ConfigError, match="VANTAGE_API_DOCS"):
        load_settings({"VANTAGE_API_DOCS": "maybe"})
