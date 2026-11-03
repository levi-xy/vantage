"""The maintenance lock, the backend heartbeat and maintenance mode (P0-6)."""

import gzip
import json
import re
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.alerts.selfcheck import HEALTH_PRIORITY, Health, SelfMonitor
from app.api.deps import CSRF_HEADER
from app.collectors.hub import EventHub
from app.core import maintenance
from app.core.db import make_engine, make_session_factory, utcnow
from app.core.maintenance import HEARTBEAT_KEY, LOCK_KEY
from app.models import ActorType, AuditLog, Setting
from tests.conftest import BACKEND_DIR, alembic_config
from tests.test_auth import ADMIN, PASSWORD, do_setup, make_client
from tests.test_backup_restore import NEW_KEY_B64, run_cli

API = "/api/v1"


def row(engine: Engine, key: str) -> object:
    with Session(engine) as db:
        return db.scalar(select(Setting.value).where(Setting.key == key))


def put_lock(engine: Engine, operation: str = "restore") -> None:
    """A lock as a CLI command (perhaps one that was killed) leaves it."""
    with Session(engine) as db:
        value = maintenance.marker(operation, field="operation", now=utcnow())
        db.add(Setting(key=LOCK_KEY, value=value))
        db.commit()


def put_heartbeat(engine: Engine, age: timedelta) -> None:
    value = maintenance.marker(maintenance.RUNNING, field="state", now=utcnow() - age)
    maintenance.write_heartbeat(engine, value)


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    path = tmp_path / "master.key.new"
    path.write_text(NEW_KEY_B64 + "\n")
    return str(path)


# --- the CLI -----------------------------------------------------------------------------


def test_a_held_lock_refuses_a_second_command(engine: Engine, db_url: str, key_file: str) -> None:
    put_lock(engine, "restore")
    held = row(engine, LOCK_KEY)
    run = run_cli(db_url, "rotate-master-key", "--new-key-file", key_file)
    assert run.code == 1
    assert "maintenance in progress: restore since" in run.err
    assert "maintenance-unlock" in run.err
    assert row(engine, LOCK_KEY) == held  # the holder keeps its lock


def test_a_fresh_heartbeat_refuses_and_releases_the_lock(
    engine: Engine, db_url: str, key_file: str
) -> None:
    put_heartbeat(engine, timedelta(seconds=5))
    for argv in (["rotate-master-key", "--new-key-file", key_file], ["restore", "-"]):
        run = run_cli(db_url, *argv)
        assert run.code == 1
        assert "the backend is running" in run.err and "docker compose stop backend" in run.err
        assert row(engine, LOCK_KEY) is None

    # A stale heartbeat (a backend that was killed) doesn't block; the lock is released
    # after the command, on success too.
    put_heartbeat(engine, timedelta(seconds=40))
    run = run_cli(db_url, "rotate-master-key", "--new-key-file", key_file)
    assert run.code == 0, run.err
    assert row(engine, LOCK_KEY) is None


def test_unlock_prints_the_holder(engine: Engine, db_url: str) -> None:
    put_lock(engine, "rotate-master-key")
    run = run_cli(db_url, "maintenance-unlock")
    assert run.code == 0, run.err
    assert re.search(
        r"Removed the maintenance lock held by rotate-master-key since \S+ \(\S+, pid \d+\)",
        run.out,
    )
    assert row(engine, LOCK_KEY) is None
    with Session(engine) as db:
        (entry,) = db.scalars(select(AuditLog))
        assert entry.actor_type == ActorType.CLI and entry.target_type == "maintenance_lock"
    assert "No maintenance lock is held." in run_cli(db_url, "maintenance-unlock").out


# --- the startup gate --------------------------------------------------------------------


def test_the_gate_runs_before_the_migrations() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text()
    assert "python -m app.cli startup-gate && alembic upgrade head && exec uvicorn" in dockerfile


def test_the_gate_with_a_lock_exits_1_and_nothing_is_migrated(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0009")
    engine = make_engine(db_url)
    try:
        put_lock(engine, "restore")
        run = run_cli(db_url, "startup-gate")
        assert run.code == 1
        assert "maintenance in progress: restore since" in run.err
        assert "Start Vantage again when it finishes" in run.err
        assert "app.cli maintenance-unlock" in run.err
        # The container command stops here: `startup-gate && alembic upgrade head`.
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0009"
        assert row(engine, HEARTBEAT_KEY) is None  # a refused start leaves no heartbeat

        assert run_cli(db_url, "maintenance-unlock").code == 0
        assert run_cli(db_url, "startup-gate").code == 0
        beat = row(engine, HEARTBEAT_KEY)
        assert isinstance(beat, dict) and beat["state"] == "starting"
    finally:
        engine.dispose()


def test_the_gate_passes_a_fresh_install(tmp_path: Path) -> None:
    missing = tmp_path / "new.db"
    assert run_cli(f"sqlite:///{missing}", "startup-gate").code == 0
    assert not missing.exists()  # never creates a database


# --- the handshake -----------------------------------------------------------------------


@pytest.mark.parametrize("first", ["cli", "backend"])
def test_the_handshake_never_lets_both_proceed(engine: Engine, first: str) -> None:
    """Both sides write their marker (in the given order), then both read: each sees the
    other's, so they never both go ahead."""
    wrote = {"cli": threading.Event(), "backend": threading.Event()}
    barrier = threading.Barrier(2, timeout=10)
    proceeded: dict[str, bool] = {}
    errors: list[BaseException] = []

    def after_write(side: str) -> None:
        wrote[side].set()
        barrier.wait()  # both have written; now both read

    def cli() -> None:
        if first == "backend":
            assert wrote["backend"].wait(10)
        try:
            maintenance.acquire(engine, "restore", after_write=lambda: after_write("cli"))
            proceeded["cli"] = True
        except maintenance.BackendRunning:
            proceeded["cli"] = False

    def backend() -> None:
        if first == "cli":
            assert wrote["cli"].wait(10)
        holder = maintenance.startup_gate(engine, after_write=lambda: after_write("backend"))
        proceeded["backend"] = holder is None

    def run(target: object) -> None:
        try:
            target()  # type: ignore[operator]
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(t,)) for t in (cli, backend)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert set(proceeded) == {"cli", "backend"}
    # Never both. Both refused is the usual outcome; one may still go on when the other
    # side, refused, has already removed its marker again before this side reads.
    assert not (proceeded["cli"] and proceeded["backend"])
    # A refused CLI released its lock; one that went on holds it.
    assert (row(engine, LOCK_KEY) is not None) == proceeded["cli"]


# --- the running backend -----------------------------------------------------------------


def test_heartbeat_interval() -> None:
    assert maintenance.HEARTBEAT_SECONDS == 10
    assert maintenance.FRESH_FOR == timedelta(seconds=30)


def test_a_lock_while_running_pauses_and_refuses_writes(engine: Engine, db_url: str) -> None:
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        state = c.app.state  # type: ignore[attr-defined]
        assert c.get("/healthz").json() == {"status": "ok"}
        assert state.poller._supervisor is not None

        put_lock(engine, "restore")
        assert c.portal is not None
        assert c.portal.call(state.heartbeat.beat) is True  # the next heartbeat sees it

        assert (c.get("/healthz").status_code, c.get("/healthz").json()) == (
            503,
            {"reason": "maintenance"},
        )
        new_user = {"username": "viewer1", "password": PASSWORD, "role": "viewer"}
        for r in (c.post(f"{API}/users", json=new_user), c.post(f"{API}/_test/admin")):
            assert r.status_code == 503 and r.json()["reason"] == "maintenance"
        # Reads and login keep working.
        assert c.get(f"{API}/me").status_code == 200
        assert c.get(f"{API}/users").status_code == 200
        login = c.post(f"{API}/auth/login", json={"username": ADMIN, "password": PASSWORD})
        assert login.status_code == 200
        c.headers[CSRF_HEADER] = login.json()["csrf_token"]
        # Polling, evaluation and delivery are paused.
        assert state.poller._supervisor is None
        assert state.alert_evaluator._task is None
        assert state.delivery._task is None

        # The lock goes away: everything resumes.
        assert run_cli(db_url, "maintenance-unlock").code == 0
        assert c.portal.call(state.heartbeat.beat) is False
        assert c.get("/healthz").json() == {"status": "ok"}
        assert state.poller._supervisor is not None
        assert state.alert_evaluator._task is not None
        assert c.post(f"{API}/users", json=new_user).status_code == 201


def test_healthz_maintenance_comes_first(engine: Engine) -> None:
    assert HEALTH_PRIORITY[0] == Health.MAINTENANCE
    monitor = SelfMonitor(session_factory=make_session_factory(engine), hub=EventHub())
    monitor.key_mismatch = True
    monitor.maintenance = True
    assert monitor.health() == Health.MAINTENANCE


def test_clean_shutdown_deletes_the_heartbeat(
    engine: Engine, db_url: str, tmp_path: Path, key_file: str
) -> None:
    with make_client(db_url):
        beat = row(engine, HEARTBEAT_KEY)
        assert isinstance(beat, dict) and beat["state"] == "running"
        # A backup taken while the backend runs leaves the runtime rows out.
        target = tmp_path / "b.json.gz"
        assert run_cli(db_url, "backup", str(target)).code == 0
        doc = json.loads(gzip.decompress(target.read_bytes()))
        settings = doc["tables"]["settings"]
        at = settings["columns"].index("key")
        keys = [r[at] for r in settings["rows"]]
        assert HEARTBEAT_KEY not in keys
    assert row(engine, HEARTBEAT_KEY) is None
    # So an offline command runs at once after `docker compose stop backend`.
    run = run_cli(db_url, "rotate-master-key", "--new-key-file", key_file)
    assert run.code == 0, run.err
