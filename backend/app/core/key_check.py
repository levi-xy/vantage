"""The master-key check value (P0-6): does the running master key match the stored secrets?

A ``settings`` row ``key_check`` holds a fixed, known value encrypted with the master key
under its own row context (``row_context("settings", "key_check")``), so it can't be
swapped with another blob. Decrypting it tells whether the running key is the one the
stored secrets were written with, without touching a single real secret.

Upgrade (no ``key_check`` row yet): every stored secret blob is tried with the current
key. All decrypt (or there are none): the row is created. None decrypt: ``wrong_key``.
Some decrypt: ``mixed_key``. Only counts are ever reported, never values.

Every secret write calls :func:`require` inside its own transaction, so a database can
never end up with secrets written under two keys.
"""

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import Connection, Table, select
from sqlalchemy.orm import Session

from app.core.crypto import DecryptionError, SecretBox, row_context
from app.models import Channel, Instance, Setting

KEY_CHECK_KEY = "key_check"
KEY_CHECK_CONTEXT = row_context("settings", KEY_CHECK_KEY)
# Not a secret: a fixed value whose only job is to decrypt (or not) with the running key.
_KNOWN_VALUE = b"vantage key check v1"

# Every table with an encrypted ``secrets`` column, bound to row_context(table, id).
ENCRYPTED_MODELS: tuple[type[Instance] | type[Channel], ...] = (Instance, Channel)


class Mismatch(StrEnum):
    """Why the key doesn't match (log lines and the system alert; /healthz says only
    ``key_mismatch``)."""

    WRONG_KEY = "wrong_key"
    MIXED_KEY = "mixed_key"


class KeyMismatchError(Exception):
    """A secret write refused: the running master key doesn't match the stored secrets."""

    def __init__(self) -> None:
        super().__init__("the master key doesn't match the stored secrets")


@dataclass(frozen=True)
class KeyCheck:
    mismatch: Mismatch | None
    # table -> (stored blobs, blobs that decrypt); filled when the row was missing or
    # didn't decrypt.
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    created: bool = False

    @property
    def ok(self) -> bool:
        return self.mismatch is None

    def describe(self) -> str:
        """Per-table counts for a log line, e.g. ``instances 2/3, channels 0/1``."""
        return ", ".join(f"{t} {good}/{total} decrypt" for t, (total, good) in self.counts.items())


def encrypted_tables() -> list[Table]:
    return [cast(Table, m.__table__) for m in ENCRYPTED_MODELS]


def seal(box: SecretBox) -> dict[str, Any]:
    """The ``key_check`` row's value for ``box``'s key."""
    blob = box.encrypt(_KNOWN_VALUE, context=KEY_CHECK_CONTEXT)
    return {"v": 1, "blob": base64.b64encode(blob).decode("ascii")}


def opens(box: SecretBox, value: Any) -> bool:
    """Whether a stored ``key_check`` value decrypts to the known value with ``box``."""
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        return False
    try:
        blob = base64.b64decode(value["blob"], validate=True)
        return box.decrypt(blob, context=KEY_CHECK_CONTEXT) == _KNOWN_VALUE
    except (binascii.Error, ValueError, DecryptionError):
        return False


def blob_counts(conn: Connection | Session, box: SecretBox) -> dict[str, tuple[int, int]]:
    """``{table: (stored blobs, blobs that decrypt with box)}``."""
    counts: dict[str, tuple[int, int]] = {}
    for table in encrypted_tables():
        query = select(table.c.id, table.c.secrets).where(table.c.secrets.is_not(None))
        total = good = 0
        for row_id, blob in conn.execute(query):
            total += 1
            try:
                box.decrypt(cast(bytes, blob), context=row_context(table.name, row_id))
            except DecryptionError:
                continue
            good += 1
        counts[table.name] = (total, good)
    return counts


def classify(counts: dict[str, tuple[int, int]]) -> Mismatch | None:
    """The upgrade rule: all decrypt (or none stored) is fine, none is the wrong key,
    some is a mixed-key database."""
    total = sum(t for t, _ in counts.values())
    good = sum(g for _, g in counts.values())
    if good == total:
        return None
    return Mismatch.WRONG_KEY if good == 0 else Mismatch.MIXED_KEY


def check(db: Session, box: SecretBox, *, create: bool = True) -> KeyCheck:
    """Check the running key against the database. With ``create``, a missing row is
    created (flushed, not committed) when every stored blob decrypts."""
    row = db.get(Setting, KEY_CHECK_KEY)
    if row is not None:
        if opens(box, row.value):
            return KeyCheck(None)
        return KeyCheck(Mismatch.WRONG_KEY, blob_counts(db, box))
    counts = blob_counts(db, box)
    mismatch = classify(counts)
    if mismatch is None and create:
        db.add(Setting(key=KEY_CHECK_KEY, value=seal(box)))
        db.flush()
        return KeyCheck(None, counts, created=True)
    return KeyCheck(mismatch, counts)


def require(db: Session, box: SecretBox) -> None:
    """Call before writing a secret, in the same transaction. Raises
    :class:`KeyMismatchError` (the API answers 503 ``key_mismatch``)."""
    if not check(db, box, create=True).ok:
        raise KeyMismatchError()
