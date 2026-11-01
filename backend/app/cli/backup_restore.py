"""Backup, restore and master-key rotation (M8.2), used by the admin CLI.

Backup file format (version 1): one gzip-compressed JSON document::

    {
      "format": "vantage-backup",
      "format_version": 1,
      "alembic_revision": "0004",
      "created_at": "2026-10-30T12:00:00+00:00",
      "tables": {"users": {"columns": ["id", ...], "rows": [[1, ...], ...]}, ...}
    }

Every table of ``Base.metadata`` is in it (``alembic_version`` is not: its value is
``alembic_revision``). Values are plain JSON: datetimes as ISO 8601 with offset, UUIDs as
strings, binary columns (the encrypted ``secrets`` blobs) as base64, JSON columns as is.
The blobs are copied as stored, still encrypted with the master key, which is never part
of a backup. The file still holds password hashes and the audit log: it is sensitive.

Restoring parses strictly: no pickle, no eval, only ``json`` with NaN/Infinity refused,
a cap on the decompressed size, exactly the known tables and columns, and every value
checked against its column's type. Rows are written with parameterised inserts through
the app's own table definitions, so a crafted file can only ever put values into
Vantage's tables, and the whole restore is one transaction.

Messages never contain a secret, a key, a value from the backup or the database URL.
"""

import base64
import binascii
import gzip
import hmac
import json
import re
import uuid
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any, NoReturn, cast

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Connection,
    Engine,
    Integer,
    LargeBinary,
    String,
    Table,
    Uuid,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.core import audit
from app.core.crypto import DecryptionError, SecretBox, row_context
from app.core.db import Base, UTCDateTime
from app.models import Channel, Instance

FORMAT = "vantage-backup"
FORMAT_VERSION = 1
# Decompressed size cap: a crafted file can't make restore use unbounded memory.
MAX_BACKUP_BYTES = 1 << 30
_REVISION = re.compile(r"\d{4}")
_TOP_LEVEL_KEYS = frozenset(
    {"format", "format_version", "alembic_revision", "created_at", "tables"}
)

# Every table with an encrypted ``secrets`` column, bound to row_context(table, id).
# Rotation refuses to run if a model gains a binary column that isn't listed here
# (see check_encrypted_columns), so a new kind of secret can't be left on the old key.
ENCRYPTED_MODELS: tuple[type[Instance] | type[Channel], ...] = (Instance, Channel)

KEY_REMINDER = (
    "The master key is NOT in the backup. Without it the device and channel credentials "
    "in the backup can't be decrypted. Back up deploy/secrets/master.key separately, and "
    "keep it apart from the backup files."
)


def _encrypted_tables() -> list[Table]:
    return [cast(Table, m.__table__) for m in ENCRYPTED_MODELS]


class BackupError(Exception):
    """A failure to report to the operator. The message never contains a secret."""


def tables() -> list[Table]:
    """Vantage's tables, parents before children (the order rows are inserted in)."""
    return list(Base.metadata.sorted_tables)


# --- column values -----------------------------------------------------------------


def _kind(column: Column[Any]) -> str:
    t = column.type
    if isinstance(t, UTCDateTime):
        return "datetime"
    if isinstance(t, Uuid):
        return "uuid"
    if isinstance(t, LargeBinary):
        return "bytes"
    if isinstance(t, JSON):
        return "json"
    if isinstance(t, Boolean):
        return "bool"
    if isinstance(t, Integer):
        return "int"
    if isinstance(t, String):  # also Text and the str_enum columns
        return "str"
    raise TypeError(f"no backup encoding for column {column.table.name}.{column.name}")


def _encode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "datetime":
        return value.isoformat()
    if kind == "uuid":
        return str(value)
    if kind == "bytes":
        return base64.b64encode(value).decode("ascii")
    if kind == "str":
        return str(value)  # StrEnum members give their value
    return value


def _decode(kind: str, value: Any, where: str) -> Any:
    def bad() -> NoReturn:
        # Never the value itself: it may be anything, including a pasted secret.
        raise BackupError(f"the backup has an invalid value in {where}")

    if value is None or kind == "json":
        return value
    if kind == "datetime":
        if not isinstance(value, str):
            bad()
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            bad()
        if parsed.tzinfo is None:
            bad()
        return parsed.astimezone(UTC)
    if kind == "uuid":
        if not isinstance(value, str):
            bad()
        try:
            return uuid.UUID(value)
        except ValueError:
            bad()
    if kind == "bytes":
        if not isinstance(value, str):
            bad()
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            bad()
    if kind == "bool":
        if not isinstance(value, bool):
            bad()
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            bad()
        return value
    if not isinstance(value, str):
        bad()
    return value


# --- revisions and locks -----------------------------------------------------------


def db_revision(conn: Connection) -> str:
    """The database's Alembic revision."""
    try:
        found: list[object] = list(
            conn.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
    except SQLAlchemyError:
        raise BackupError(
            "the database has no migration revision: start the backend once (it applies "
            "the migrations), then try again"
        ) from None
    if len(found) != 1 or not isinstance(found[0], str):
        raise BackupError("the database's migration revision is unclear; nothing was changed")
    return found[0]


def _begin_snapshot(conn: Connection, *, write: bool) -> None:
    """Make the following statements one consistent transaction.

    SQLite's Python driver only opens a transaction before a write, so every SELECT would
    otherwise see the database as it is at that moment. ``BEGIN IMMEDIATE`` also takes
    the write lock up front, so the running backend can't write in between.
    """
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
    elif conn.dialect.name == "postgresql":
        if write:
            # Blocks other writers (the running backend) until commit; reads go on.
            names = ", ".join(conn.dialect.identifier_preparer.format_table(t) for t in tables())
            conn.exec_driver_sql(f"LOCK TABLE {names} IN EXCLUSIVE MODE")
        else:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


# --- backup ------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupSummary:
    revision: str
    rows: int


def write_backup(engine: Engine, out: IO[bytes]) -> BackupSummary:
    """Export every table into ``out`` as one gzip-compressed JSON document."""
    doc: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "alembic_revision": "",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tables": {},
    }
    total = 0
    with engine.connect() as conn:
        _begin_snapshot(conn, write=False)
        doc["alembic_revision"] = db_revision(conn)
        for table in tables():
            columns = list(table.columns)
            kinds = [_kind(c) for c in columns]
            pk = list(table.primary_key.columns)
            rows = [
                [_encode(k, v) for k, v in zip(kinds, row, strict=True)]
                for row in conn.execute(select(table).order_by(*pk))
            ]
            doc["tables"][table.name] = {"columns": [c.name for c in columns], "rows": rows}
            total += len(rows)
        conn.rollback()
    payload = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    out.write(gzip.compress(payload))
    return BackupSummary(revision=doc["alembic_revision"], rows=total)


# --- reading a backup --------------------------------------------------------------


@dataclass(frozen=True)
class Backup:
    revision: str
    rows: Mapping[str, list[dict[str, Any]]]  # table name -> rows as column -> value

    @property
    def row_count(self) -> int:
        return sum(len(r) for r in self.rows.values())


def _reject_constant(_name: str) -> NoReturn:
    raise ValueError("NaN and Infinity are not allowed")


def read_backup(source: IO[bytes]) -> Backup:
    """Parse and validate a backup file. Raises :class:`BackupError`."""
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as unzipped:
            data = unzipped.read(MAX_BACKUP_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        raise BackupError("not a Vantage backup file (it isn't valid gzip)") from None
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError("the backup is larger than the restore limit (1 GiB uncompressed)")
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BackupError("not a Vantage backup file (it isn't valid JSON)") from None
    return _validate(doc)


def _validate(doc: Any) -> Backup:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise BackupError("not a Vantage backup file")
    if set(doc) != _TOP_LEVEL_KEYS:
        raise BackupError("the backup file has unexpected or missing fields")
    if doc["format_version"] != FORMAT_VERSION or isinstance(doc["format_version"], bool):
        raise BackupError(
            f"unsupported backup format version (this Vantage reads version {FORMAT_VERSION})"
        )
    revision = doc["alembic_revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise BackupError("the backup's schema revision is missing or malformed")
    raw_tables = doc["tables"]
    known = {t.name: t for t in tables()}
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(known):
        raise BackupError("the backup's tables don't match this Vantage version's tables")

    rows: dict[str, list[dict[str, Any]]] = {}
    for name, table in known.items():
        entry = raw_tables[name]
        if not isinstance(entry, dict) or set(entry) != {"columns", "rows"}:
            raise BackupError(f"the backup's table {name} is malformed")
        names = entry["columns"]
        expected = {c.name for c in table.columns}
        if not isinstance(names, list) or len(names) != len(expected) or set(names) != expected:
            raise BackupError(f"the backup's table {name} has unexpected or missing columns")
        kinds = [_kind(table.columns[n]) for n in names]
        raw_rows = entry["rows"]
        if not isinstance(raw_rows, list):
            raise BackupError(f"the backup's table {name} is malformed")
        decoded: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_rows):
            if not isinstance(raw, list) or len(raw) != len(names):
                raise BackupError(f"the backup's table {name} has a malformed row ({index + 1})")
            decoded.append(
                {
                    col: _decode(kind, value, f"{name}.{col}, row {index + 1}")
                    for col, kind, value in zip(names, kinds, raw, strict=True)
                }
            )
        rows[name] = decoded
    return Backup(revision=revision, rows=rows)


# --- restore -----------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    rows: int
    secrets_total: int
    secrets_undecryptable: int


def _check_revision(backup_revision: str, current: str) -> None:
    if backup_revision == current:
        return
    if not _REVISION.fullmatch(current) or int(backup_revision) > int(current):
        raise BackupError(
            f"the backup is from a newer database schema (revision {backup_revision}; this "
            f"database is at {current}): upgrade Vantage first, then restore"
        )
    # TODO(maintainer): restoring an older backup would mean migrating the restored rows
    # (restore into the old schema, then upgrade). Refused for now.
    raise BackupError(
        f"the backup is from an older database schema (revision {backup_revision}; this "
        f"database is at {current}). Restore it with the Vantage version that made it, "
        "then upgrade"
    )


def _reset_sequences(conn: Connection) -> None:
    """Postgres: move each id sequence past the restored ids (explicit ids don't)."""
    if conn.dialect.name != "postgresql":
        return
    for table in tables():
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not isinstance(pk[0].type, Integer):
            continue
        # Built from Vantage's own table definitions, never from the file.
        highest = select(func.coalesce(func.max(pk[0]), 0)).scalar_subquery()
        sequence = func.pg_get_serial_sequence(table.name, pk[0].name)
        conn.execute(select(func.setval(sequence, highest + 1, False)))


def count_undecryptable(conn: Connection, box: SecretBox) -> tuple[int, int]:
    """``(total, undecryptable)`` stored secret blobs for ``box``'s key."""
    total = bad = 0
    for table in _encrypted_tables():
        query = select(table.c.id, table.c.secrets).where(table.c.secrets.is_not(None))
        for row_id, blob in conn.execute(query):
            total += 1
            try:
                box.decrypt(cast(bytes, blob), context=row_context(table.name, row_id))
            except DecryptionError:
                bad += 1
    return total, bad


def restore(engine: Engine, backup: Backup, box: SecretBox, *, overwrite: bool) -> RestoreResult:
    """Import ``backup`` in one transaction. Raises :class:`BackupError`."""
    with engine.connect() as conn:
        _begin_snapshot(conn, write=True)
        _check_revision(backup.revision, db_revision(conn))
        existing = sum(
            conn.execute(select(func.count()).select_from(t)).scalar_one() for t in tables()
        )
        if existing and not overwrite:
            raise BackupError(
                f"the database is not empty ({existing} row(s)): restore into a freshly "
                "started Vantage, or pass --overwrite to replace everything in it"
            )
        if existing:
            for table in reversed(tables()):
                conn.execute(table.delete())
        for table in tables():
            if backup.rows[table.name]:
                conn.execute(table.insert(), backup.rows[table.name])
        _reset_sequences(conn)
        with Session(bind=conn) as db:
            audit.record(
                db,
                actor_type=audit.ActorType.CLI,
                action=audit.Action.UPDATE,
                target_type="database",
                target_id=None,
                user_id=None,
                changes={
                    "restored_rows": backup.row_count,
                    "backup_revision": backup.revision,
                    "replaced_rows": existing,
                    "source": "cli",
                },
            )
            db.flush()
        total, bad = count_undecryptable(conn, box)
        conn.commit()
    return RestoreResult(rows=backup.row_count, secrets_total=total, secrets_undecryptable=bad)


# --- master-key rotation -----------------------------------------------------------


def check_encrypted_columns() -> None:
    """Refuse if a table has a binary column rotation doesn't know how to re-encrypt."""
    listed = {t.name for t in _encrypted_tables()}
    for table in tables():
        for column in table.columns:
            if isinstance(column.type, LargeBinary) and not (
                table.name in listed and column.name == "secrets"
            ):
                raise BackupError(
                    f"{table.name}.{column.name} may hold encrypted data that rotation doesn't "
                    "cover; nothing was changed"
                )


def rotate_master_key(engine: Engine, old: SecretBox, new: SecretBox) -> dict[str, int]:
    """Re-encrypt every stored secret blob from ``old`` to ``new``, all or nothing.

    Returns the number of blobs re-encrypted per table. Raises :class:`BackupError`
    (nothing changed) if any blob doesn't decrypt with the old key or the result doesn't
    decrypt with the new one.
    """
    check_encrypted_columns()
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        _begin_snapshot(conn, write=True)
        for table in _encrypted_tables():
            query = select(table.c.id, table.c.secrets).where(table.c.secrets.is_not(None))
            done = 0
            for row_id, blob in conn.execute(query).all():
                context = row_context(table.name, row_id)
                try:
                    plaintext = old.decrypt(cast(bytes, blob), context=context)
                except DecryptionError:
                    raise BackupError(
                        f"a stored secret in {table.name} can't be decrypted with the current "
                        "master key, so it can't be re-encrypted; nothing was changed"
                    ) from None
                fresh = new.encrypt(plaintext, context=context)
                conn.execute(update(table).where(table.c.id == row_id).values(secrets=fresh))
                done += 1
            counts[table.name] = done
        # Read back what is about to be committed: every blob must open with the new key.
        total, bad = count_undecryptable(conn, new)
        if bad or total != sum(counts.values()):
            raise BackupError("re-encrypted secrets failed verification; nothing was changed")
        with Session(bind=conn) as db:
            audit.record(
                db,
                actor_type=audit.ActorType.CLI,
                action=audit.Action.UPDATE,
                target_type="master_key",
                target_id=None,
                user_id=None,
                changes={"re_encrypted": counts, "source": "cli"},
            )
            db.flush()
        conn.commit()
    return counts


def same_key(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)
