import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, MetaData
from sqlalchemy.orm import Session

from app.core.db import make_engine, make_session_factory

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Set in CI to run DB tests against Postgres as well as SQLite.
POSTGRES_URL = os.environ.get("VANTAGE_TEST_POSTGRES_URL")

_BACKENDS = ["sqlite"] + (["postgres"] if POSTGRES_URL else [])


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def _wipe(engine: Engine) -> None:
    meta = MetaData()
    meta.reflect(engine)
    meta.drop_all(engine)


@pytest.fixture(params=_BACKENDS)
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return
    assert POSTGRES_URL is not None
    engine = make_engine(POSTGRES_URL)
    _wipe(engine)
    try:
        yield POSTGRES_URL
    finally:
        _wipe(engine)
        engine.dispose()


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """An engine on a database migrated to the latest revision."""
    command.upgrade(alembic_config(db_url), "head")
    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s
