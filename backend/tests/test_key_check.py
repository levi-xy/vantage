"""The master-key check value and key_mismatch mode (P0-6).

Security assertions: in key_mismatch mode no secret is written (instances, channels), a
mixed-key database can't be produced by restore, and no secret value reaches a response,
a log line or a stored notification.
"""

import base64
import io
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.alerts import channels as ch
from app.alerts.key_monitor import KeyMonitor
from app.alerts.selfcheck import SelfMonitor
from app.api.deps import CSRF_HEADER
from app.cli import Console, main
from app.collectors.hub import EventHub
from app.core import key_check
from app.core.auth import Passwords
from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox, row_context
from app.core.db import make_session_factory, utcnow
from app.core.key_check import KEY_CHECK_KEY, Mismatch
from app.integrations import instance_config as ic
from app.main import create_app
from app.models import (
    AlertEvent,
    AlertNotification,
    AlertState,
    AuditLog,
    Channel,
    ChannelType,
    Instance,
    NotificationStatus,
    Setting,
    SystemKind,
)
from tests.conftest import TEST_MASTER_KEY
from tests.test_auth import FAST_HASHER, do_setup, make_client
from tests.test_instances import REGISTRY, body, fake_guards

API = "/api/v1"
OTHER_KEY = b"vantage-other-key-not-a-secret-0"
INSTANCE_SECRET = "example-token-instance-kc"
BOT_TOKEN = "123456789:example-token-telegram-kc000000"
SECRETS = (INSTANCE_SECRET, BOT_TOKEN, "example-token-7f3a91c2")


def add_instance(db: Session, key: bytes) -> None:
    inst_id = uuid.uuid4()
    db.add(
        Instance(
            id=inst_id,
            type_id="fake_device",
            name=f"dev-{inst_id.hex[:6]}",
            config={"host": "10.0.0.10", "port": 8443},
            secrets=ic.encrypt(SecretBox(key), inst_id, {"api_token": INSTANCE_SECRET}),
        )
    )


def add_channel(db: Session, key: bytes) -> Channel:
    row = Channel(type=ChannelType.TELEGRAM, name="ops", config={"chat_id": "1"})
    db.add(row)
    db.flush()
    row.secrets = ch.encrypt(SecretBox(key), row.id, {"bot_token": BOT_TOKEN})
    return row


def seal_with(db: Session, key: bytes) -> None:
    db.add(Setting(key=KEY_CHECK_KEY, value=key_check.seal(SecretBox(key))))


def assert_no_secrets(text: str) -> None:
    for value in SECRETS:
        assert value not in text


# --- the check value -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ((), None),  # no secrets stored
        ((TEST_MASTER_KEY, TEST_MASTER_KEY), None),  # all decrypt
        ((OTHER_KEY, OTHER_KEY), Mismatch.WRONG_KEY),  # none decrypt
        ((TEST_MASTER_KEY, OTHER_KEY), Mismatch.MIXED_KEY),  # some decrypt
    ],
)
def test_upgrade_without_a_key_check_row(
    session: Session, secret_box: SecretBox, keys: tuple[bytes, ...], expected: Mismatch | None
) -> None:
    if keys:
        add_instance(session, keys[0])
        add_channel(session, keys[1])
    session.commit()

    result = key_check.check(session, secret_box)
    session.commit()

    assert result.mismatch == expected
    row = session.get(Setting, KEY_CHECK_KEY)
    if expected is None:
        assert row is not None and key_check.opens(secret_box, row.value)
        assert key_check.check(session, secret_box).ok  # from now on, from the row
        # Another key fails, unless no secret is stored (then it is sealed again).
        assert key_check.check(session, SecretBox(OTHER_KEY)).ok == (not keys)
    else:
        assert row is None  # never created on a key that doesn't open everything
    assert_no_secrets(result.describe())


def test_the_value_is_bound_to_its_row(session: Session, secret_box: SecretBox) -> None:
    # A real secret blob put in its place doesn't pass, even though it decrypts elsewhere.
    blob = secret_box.encrypt(b"vantage key check v1", context=row_context("instances", "x"))

    value = {"v": 1, "blob": base64.b64encode(blob).decode()}
    assert not key_check.opens(secret_box, value)
    assert key_check.opens(secret_box, key_check.seal(secret_box))


# --- rotation and restore --------------------------------------------------------------


def run_cli(db_url: str, *argv: str, key: bytes = TEST_MASTER_KEY) -> int:

    out = io.StringIO()
    return main(
        list(argv),
        console=Console(stdin=io.StringIO(""), stdout=out, stderr=out),
        settings=Settings(database_url=normalize_database_url(db_url)),
        passwords=Passwords(FAST_HASHER),
        master_key=key,
    )


def test_rotation_then_a_backend_on_the_old_key_is_in_key_mismatch(
    engine: Engine, db_url: str, tmp_path: Path, secret_box: SecretBox
) -> None:
    with make_session_factory(engine)() as db:
        add_channel(db, TEST_MASTER_KEY)
        seal_with(db, TEST_MASTER_KEY)
        db.commit()
    key_file = tmp_path / "master.key.new"

    key_file.write_text(base64.b64encode(OTHER_KEY).decode())
    assert run_cli(db_url, "rotate-master-key", "--new-key-file", str(key_file)) == 0

    with make_session_factory(engine)() as db:
        assert key_check.check(db, SecretBox(OTHER_KEY)).ok
        assert key_check.check(db, secret_box).mismatch == Mismatch.WRONG_KEY
    with make_client(db_url) as c:  # make_client runs on TEST_MASTER_KEY, the old key
        r = c.get("/healthz")
        assert (r.status_code, r.json()) == (503, {"reason": "key_mismatch"})


def test_restore_refuses_a_mixed_key_backup(engine: Engine, db_url: str, tmp_path: Path) -> None:
    with make_session_factory(engine)() as db:
        add_instance(db, TEST_MASTER_KEY)
        add_channel(db, OTHER_KEY)
        db.commit()
    target = tmp_path / "b.json.gz"
    assert run_cli(db_url, "backup", str(target)) == 0
    with make_session_factory(engine)() as db:
        before = db.scalars(select(Instance.id)).all()
    assert run_cli(db_url, "restore", "--overwrite", str(target)) == 1
    with make_session_factory(engine)() as db:
        assert db.scalars(select(Instance.id)).all() == before
        assert db.get(Setting, KEY_CHECK_KEY) is None


# --- key_mismatch mode through the API -------------------------------------------------


@pytest.fixture
def mismatched(engine: Engine) -> int:
    """A database whose key check value and channel are on another key: one Telegram
    channel, nothing else. Returns the channel id."""
    with make_session_factory(engine)() as db:
        channel = add_channel(db, OTHER_KEY)
        seal_with(db, OTHER_KEY)
        db.commit()
        return channel.id


def test_wrong_key_with_only_telegram(
    engine: Engine, db_url: str, mismatched: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with make_client(db_url) as c:
        r = c.get("/healthz")
        assert (r.status_code, r.json()) == (503, {"reason": "key_mismatch"})
        assert not c.app.state.poller._supervisor  # type: ignore[attr-defined]  # polls stopped
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]  # logins and reads still work
        status = c.get(f"{API}/alerts/delivery-status").json()
        assert [a["system_kind"] for a in status["system_alerts"]] == ["key_mismatch"]
        (event,) = c.get(f"{API}/alerts/events").json()
        assert [(d["channel_id"], d["status"], d["error_kind"]) for d in event["deliveries"]] == [
            (mismatched, "failed", "credentials_unreadable")
        ]
        texts = [r.text, c.get(f"{API}/channels").text, c.get(f"{API}/alerts/events").text]
    with make_session_factory(engine)() as db:
        (row,) = db.scalars(select(AlertNotification))
        assert row.finished_at is not None and BOT_TOKEN not in row.text
    assert "master key mismatch" in caplog.text and "channels 0/1 decrypt" in caplog.text
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)
    assert_no_secrets(caplog.text + "".join(texts))


def test_instance_secret_write_is_refused(engine: Engine, db_url: str, mismatched: int) -> None:
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        r = c.post(f"{API}/instances", json=body())
        assert r.status_code == 503 and r.json()["reason"] == "key_mismatch"
        assert_no_secrets(r.text)
    with make_session_factory(engine)() as db:
        assert db.scalars(select(Instance)).all() == []
        assert db.scalars(select(AuditLog).where(AuditLog.target_type == "instance")).all() == []


def test_channel_secret_writes_are_refused(engine: Engine, db_url: str, mismatched: int) -> None:
    with make_session_factory(engine)() as db:
        stored = db.get(Channel, mismatched)
        assert stored is not None
        blob = stored.secrets
    new = {"type": "telegram", "name": "new", "config": {"chat_id": "2", "bot_token": BOT_TOKEN}}
    with make_client(db_url) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        created = c.post(f"{API}/channels", json=new)
        patched = c.patch(f"{API}/channels/{mismatched}", json={"config": {"bot_token": BOT_TOKEN}})
        for r in (created, patched):
            assert r.status_code == 503 and r.json()["reason"] == "key_mismatch"
            assert_no_secrets(r.text)
    with make_session_factory(engine)() as db:
        (only,) = db.scalars(select(Channel))
        assert only.id == mismatched and only.secrets == blob


def test_instance_secret_update_is_refused(engine: Engine, db_url: str, mismatched: int) -> None:
    with make_session_factory(engine)() as db:
        add_instance(db, OTHER_KEY)
        db.commit()
        (row,) = db.scalars(select(Instance))
        inst_id, blob = row.id, row.secrets
    with make_client(db_url, registry=REGISTRY, net_guard_factory=fake_guards) as c:
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        r = c.patch(f"{API}/instances/{inst_id}", json={"config": {"api_token": BOT_TOKEN}})
        assert r.status_code == 503 and r.json()["reason"] == "key_mismatch"
        assert_no_secrets(r.text)
    with make_session_factory(engine)() as db:
        stored = db.get(Instance, inst_id)
        assert stored is not None and stored.secrets == blob


def test_no_secrets_left_reseals_the_check_value(
    engine: Engine, db_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """After a lost key: once the unreadable devices and channels are deleted, nothing
    is on the old key any more, so the check value takes the running key."""
    caplog.set_level(logging.WARNING)
    with make_session_factory(engine)() as db:
        seal_with(db, OTHER_KEY)
        db.commit()
    target = tmp_path / "b.json.gz"
    assert run_cli(db_url, "backup", str(target)) == 0  # carries the OTHER_KEY row

    with make_client(db_url) as c:  # TEST_MASTER_KEY
        assert c.get("/healthz").json() == {"status": "ok"}
    assert "sealed again with the running key" in caplog.text
    with make_session_factory(engine)() as db:
        row = db.get(Setting, KEY_CHECK_KEY)
        assert row is not None and key_check.opens(SecretBox(TEST_MASTER_KEY), row.value)

    # Restore of a backup without secrets does the same.

    assert run_cli(db_url, "restore", "--overwrite", str(target)) == 0
    with make_session_factory(engine)() as db:
        row = db.get(Setting, KEY_CHECK_KEY)
        assert row is not None and key_check.opens(SecretBox(TEST_MASTER_KEY), row.value)


def test_restart_with_the_right_key_resolves_the_alert_left_open(
    engine: Engine, db_url: str, mismatched: int
) -> None:
    with make_client(db_url) as c:  # TEST_MASTER_KEY: the wrong key for this database
        assert c.get("/healthz").json() == {"reason": "key_mismatch"}
    # The backend restarts with the right key (the fixture's data is on OTHER_KEY).
    app = create_app(
        Settings(database_url=normalize_database_url(db_url)),
        secret_box=SecretBox(OTHER_KEY),
        passwords=Passwords(FAST_HASHER),
    )
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}  # failed rows no longer count
    with make_session_factory(engine)() as db:
        (event,) = db.scalars(select(AlertEvent))
        assert event.system_kind == SystemKind.KEY_MISMATCH
        assert event.state == AlertState.RESOLVED


# --- the monitor -------------------------------------------------------------------------


async def test_monitor_pauses_logs_every_5_minutes_and_recovers(
    engine: Engine, mismatched: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sessions = make_session_factory(engine)
    now = [utcnow()]
    calls: list[str] = []

    async def pause() -> None:
        calls.append("pause")

    async def resume() -> None:
        calls.append("resume")

    def clock() -> datetime:
        return now[0]

    monitor = SelfMonitor(session_factory=sessions, hub=EventHub(), clock=clock)
    keys = KeyMonitor(
        session_factory=sessions,
        secret_box=SecretBox(TEST_MASTER_KEY),
        monitor=monitor,
        pause=pause,
        resume=resume,
        clock=clock,
    )

    def errors() -> int:
        return sum(
            r.levelno == logging.ERROR and "master key mismatch (" in r.message
            for r in caplog.records
        )

    assert not await keys.check()
    assert calls == ["pause"] and monitor.health() == "key_mismatch" and errors() == 1
    now[0] += timedelta(minutes=2)
    assert not await keys.check()
    assert errors() == 1  # not repeated within 5 minutes, and no second alert
    now[0] += timedelta(minutes=3)
    assert not await keys.check()
    assert errors() == 2

    with sessions() as db:  # e.g. a restore made with this server's key
        row = db.get(Setting, KEY_CHECK_KEY)
        assert row is not None
        row.value = key_check.seal(SecretBox(TEST_MASTER_KEY))
        db.commit()
    assert await keys.check()
    # The failed FIRING message stays failed, but the RESOLVED message stands in for it:
    # it doesn't count as a stuck notification.
    assert calls == ["pause", "resume"] and monitor.health() is None
    with sessions() as db:
        (event,) = db.scalars(select(AlertEvent))
        assert event.system_kind == SystemKind.KEY_MISMATCH
        assert event.state == AlertState.RESOLVED
        kinds = sorted(
            (n.kind.value, n.status.value) for n in db.scalars(select(AlertNotification))
        )
        assert kinds == [("firing", "failed"), ("resolved", NotificationStatus.PENDING.value)]
    assert_no_secrets(caplog.text)
