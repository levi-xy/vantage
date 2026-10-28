import asyncio
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, MetaData
from sqlalchemy.orm import Session

from app.core.crypto import SecretBox
from app.core.db import make_engine, make_session_factory

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Set in CI to run DB tests against Postgres as well as SQLite.
POSTGRES_URL = os.environ.get("VANTAGE_TEST_POSTGRES_URL")

# Obviously fake, fixed 32-byte key for tests. Never use outside tests.
TEST_MASTER_KEY = b"vantage-test-key-not-a-secret-00"

_BACKENDS = ["sqlite"] + (["postgres"] if POSTGRES_URL else [])

# --- event loops -----------------------------------------------------------------------
#
# The container runs uvicorn[standard] on uvloop, which lacks some asyncio loop APIs
# (loop.sock_recvfrom/sock_sendto raise NotImplementedError). Code that does its own
# socket I/O on the loop (the guarded ping client) runs its tests on both loops:
#
#     BOTH_LOOPS = pytest.mark.asyncio(loop_factories=["asyncio", "uvloop"])
#
# Every other async test keeps the default asyncio loop. uvloop comes with
# uvicorn[standard] except on Windows; where it is missing, the uvloop runs are skipped.
_ASYNCIO_ONLY: dict[str, Callable[[], asyncio.AbstractEventLoop]] = {
    "asyncio": asyncio.new_event_loop
}
_ALL_LOOPS = dict(_ASYNCIO_ONLY)
try:
    import uvloop
except ImportError:  # pragma: no cover - Windows
    pass
else:
    _ALL_LOOPS["uvloop"] = uvloop.new_event_loop


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    marker = item.get_closest_marker("asyncio")
    if marker is not None and "loop_factories" in marker.kwargs:
        return _ALL_LOOPS
    return _ASYNCIO_ONLY


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


@pytest.fixture
def secret_box() -> SecretBox:
    return SecretBox(TEST_MASTER_KEY)
