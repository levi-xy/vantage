"""Backup, restore and master-key rotation through the admin CLI (M8.2)."""

import base64
import gzip
import io
import json
import stat
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.cli import Console, main
from app.cli.backup_restore import KEY_REMINDER, tables
from app.core.auth import Passwords
from app.core.config import Settings, normalize_database_url
from app.core.crypto import DecryptionError, SecretBox, row_context
from app.core.db import make_session_factory
from app.models import (
    ActorType,
    AuditLog,
    Channel,
    ChannelType,
    Instance,
    Role,
    User,
    UserSession,
)
from tests.conftest import TEST_MASTER_KEY
from tests.test_auth import FAST_HASHER

INSTANCE_SECRET = "example-token-instance"
CHANNEL_SECRET = "example-token-channel"
NEW_KEY = b"vantage-new-test-key-not-secret!"
OTHER_KEY = b"vantage-other-key-not-a-secret-0"
NEW_KEY_B64 = base64.b64encode(NEW_KEY).decode()


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err
        self.all = out + err


def run_cli(db_url: str, *argv: str, key: bytes = TEST_MASTER_KEY) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        console=Console(stdin=io.StringIO(""), stdout=out, stderr=err),
        settings=Settings(database_url=normalize_database_url(db_url)),
        passwords=Passwords(FAST_HASHER),
        master_key=key,
    )
    return Run(code, out.getvalue(), err.getvalue())


def seed(engine: Engine, channel_key: bytes = TEST_MASTER_KEY) -> None:
    box = SecretBox(TEST_MASTER_KEY)
    with make_session_factory(engine)() as db:
        inst_id = uuid.uuid4()
        db.add(
            Instance(
                id=inst_id,
                type_id="generic",
                name="lab-fw",
                config={"host": "192.0.2.10"},
                secrets=box.encrypt_secrets(
                    {"token": INSTANCE_SECRET}, context=row_context("instances", inst_id)
                ),
            )
        )
        channel = Channel(type=ChannelType("telegram"), name="ops", config={"chat_id": "1"})
        db.add(channel)
        user = User(username="admin", password_hash="example-hash", role=Role.ADMIN)
        db.add(user)
        db.flush()
        db.add(
            UserSession(
                id="0" * 64,
                user_id=user.id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        channel.secrets = SecretBox(channel_key).encrypt_secrets(
            {"bot_token": CHANNEL_SECRET}, context=row_context("channels", channel.id)
        )
        db.commit()


def blobs(engine: Engine) -> dict[str, bytes | None]:
    with Session(engine) as db:
        found: dict[str, bytes | None] = {}
        for model in (Instance, Channel):
            for row in db.scalars(select(model)):
                found[f"{model.__tablename__}:{row.id}"] = row.secrets
        return found


def dump(engine: Engine) -> dict[str, list[Any]]:
    with engine.connect() as conn:
        return {
            t.name: [tuple(r) for r in conn.execute(select(t).order_by(*t.primary_key))]
            for t in tables()
        }


def audit_rows(engine: Engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


def no_secret_in(text: str) -> None:
    for secret in (INSTANCE_SECRET, CHANNEL_SECRET, NEW_KEY_B64, NEW_KEY.decode()):
        assert secret not in text
    assert TEST_MASTER_KEY.decode() not in text


def test_backup_restore_round_trip(engine: Engine, db_url: str, tmp_path: Path) -> None:
    seed(engine)
    before = dump(engine)
    target = tmp_path / "backups" / "vantage.json.gz"

    run = run_cli(db_url, "backup", str(target))
    assert run.code == 0, run.err
    assert KEY_REMINDER in run.out
    no_secret_in(run.all)
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    content = gzip.decompress(target.read_bytes())
    assert b"lab-fw" in content  # really the decompressed backup
    for secret in (INSTANCE_SECRET, CHANNEL_SECRET):
        assert secret.encode() not in content
    assert TEST_MASTER_KEY not in content
    assert base64.b64encode(TEST_MASTER_KEY) not in content

    # Never overwrites an existing backup file.
    again = run_cli(db_url, "backup", str(target))
    assert again.code == 1 and "already exists" in again.err

    # A database with data is not clobbered without --overwrite.
    refused = run_cli(db_url, "restore", str(target))
    assert refused.code == 1 and "not empty" in refused.err
    assert dump(engine) == before

    # Into an empty database: everything comes back, secrets still open with the key.
    with engine.begin() as conn:
        for table in reversed(tables()):
            conn.execute(table.delete())
    restored = run_cli(db_url, "restore", str(target))
    assert restored.code == 0, restored.err
    no_secret_in(restored.all)
    after = dump(engine)
    restore_audit = after.pop("audit_log")
    # Everything but the sessions comes back: everyone logs in again.
    assert len(before["sessions"]) == 1 and after.pop("sessions") == []
    # The backup predates the key check value (P0-6): restore created one for this key.
    assert before["settings"] == [] and [k for k, _ in after.pop("settings")] == ["key_check"]
    skipped = ("audit_log", "sessions", "settings")
    assert after == {k: v for k, v in before.items() if k not in skipped}
    assert "1 skipped" in restored.out
    assert restore_audit[: len(before["audit_log"])] == before["audit_log"]
    entry = audit_rows(engine)[-1]
    assert entry.actor_type == ActorType.CLI and entry.user_id is None
    assert entry.target_type == "database"
    assert entry.changes is not None and entry.changes["sessions_skipped"] == 1

    # --overwrite replaces a non-empty database.
    assert run_cli(db_url, "restore", "--overwrite", str(target)).code == 0


def test_restore_rejects_a_tampered_file(engine: Engine, db_url: str, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json.gz"

    bad.write_bytes(gzip.compress(json.dumps({"format": "vantage-backup"}).encode()))
    run = run_cli(db_url, "restore", str(bad))
    assert run.code == 1 and "unexpected or missing fields" in run.err

    # A value outside an enum column's choices (users.role) is refused.
    seed(engine)
    good = tmp_path / "good.json.gz"
    assert run_cli(db_url, "backup", str(good)).code == 0
    doc = json.loads(gzip.decompress(good.read_bytes()))
    users = doc["tables"]["users"]
    users["rows"][0][users["columns"].index("role")] = "superuser"
    bad.write_bytes(gzip.compress(json.dumps(doc).encode()))
    run = run_cli(db_url, "restore", "--overwrite", str(bad))
    assert run.code == 1 and "invalid value in users.role" in run.err
    assert "superuser" not in run.all


def test_rotate_master_key(engine: Engine, db_url: str, tmp_path: Path) -> None:
    seed(engine)
    key_file = tmp_path / "master.key.new"
    key_file.write_text(NEW_KEY_B64 + "\n")
    assert len(audit_rows(engine)) == 0

    run = run_cli(db_url, "rotate-master-key", "--new-key-file", str(key_file))
    assert run.code == 0, run.err
    assert "Re-encrypted 2 stored secret(s)" in run.out
    no_secret_in(run.all)

    old, new = SecretBox(TEST_MASTER_KEY), SecretBox(NEW_KEY)
    for where, blob in blobs(engine).items():
        assert blob is not None
        table, row_id = where.split(":", 1)
        context = row_context(table, row_id)
        with pytest.raises(DecryptionError):
            old.decrypt(blob, context=context)
        assert new.decrypt_secrets(blob, context=context)

    [entry] = audit_rows(engine)
    assert entry.actor_type == ActorType.CLI and entry.user_id is None
    assert entry.target_type == "master_key"
    assert entry.changes == {"re_encrypted": {"instances": 1, "channels": 1}, "source": "cli"}

    # The same key twice is refused.
    same = run_cli(db_url, "rotate-master-key", "--new-key-file", str(key_file), key=NEW_KEY)
    assert same.code == 1 and "same as the current one" in same.err


@pytest.mark.parametrize("case", ["wrong_old_key", "one_blob_on_another_key"])
def test_rotate_is_all_or_nothing(engine: Engine, db_url: str, tmp_path: Path, case: str) -> None:
    # Instances are re-encrypted before channels; a bad channel blob must undo them too.
    seed(engine, channel_key=OTHER_KEY if case == "one_blob_on_another_key" else TEST_MASTER_KEY)
    before = blobs(engine)
    key_file = tmp_path / "master.key.new"
    key_file.write_text(NEW_KEY_B64)
    current = OTHER_KEY if case == "wrong_old_key" else TEST_MASTER_KEY

    run = run_cli(db_url, "rotate-master-key", "--new-key-file", str(key_file), key=current)
    assert run.code == 1
    assert "nothing was changed" in run.err
    no_secret_in(run.all)
    assert blobs(engine) == before
    assert audit_rows(engine) == []
