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


def test_session_defaults_are_secure() -> None:
    settings = load_settings({})
    assert settings.cookie_secure is True
    assert settings.session_max_hours == 12
    assert settings.session_idle_minutes == 120


@pytest.mark.parametrize("raw", ["", "  "])
def test_empty_cookie_secure_stays_on(raw: str) -> None:
    assert load_settings({"VANTAGE_COOKIE_SECURE": raw}).cookie_secure is True


def test_cookie_secure_can_be_turned_off() -> None:
    assert load_settings({"VANTAGE_COOKIE_SECURE": "false"}).cookie_secure is False


def test_session_lengths_are_read() -> None:
    env = {"VANTAGE_SESSION_MAX_HOURS": "24", "VANTAGE_SESSION_IDLE_MINUTES": "30"}
    settings = load_settings(env)
    assert (settings.session_max_hours, settings.session_idle_minutes) == (24, 30)


@pytest.mark.parametrize(
    "name,raw",
    [
        ("VANTAGE_SESSION_MAX_HOURS", "0"),
        ("VANTAGE_SESSION_MAX_HOURS", "721"),
        ("VANTAGE_SESSION_MAX_HOURS", "twelve"),
        ("VANTAGE_SESSION_IDLE_MINUTES", "1"),
        ("VANTAGE_COOKIE_SECURE", "maybe"),
    ],
)
def test_bad_session_settings_are_rejected(name: str, raw: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({name: raw})
