"""The one-time setup code that guards first-run setup (maintainer-approved 2026-11-10).

``python -m app.cli setup-code`` (run by ``deploy/setup.sh``) makes a fresh code and
prints it; ``POST /api/v1/setup`` creates the first admin only with that code, then
deletes it in the same transaction. So the first admin can only be created by someone
who can run commands on the server, not by whoever reaches the page first.

The ``settings`` row ``setup_code`` holds only ``{"sha256", "created_at"}``, never the
code. It is runtime state: left out of backups and kept (not replaced) by a restore,
like ``maintenance_lock`` (``app.core.maintenance.RUNTIME_KEYS``).

A code is 16 characters from a 31-character alphabet without look-alikes (no 0/O,
1/I/L): about 79 bits, shown as ``ABCD-EFGH-JKMN-PQRS``. Typed variants (lower case,
missing dashes, spaces) are accepted.
"""

import hashlib
import hmac
import secrets
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import Setting

SETUP_CODE_KEY = "setup_code"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GROUPS = 4
GROUP_LENGTH = 4
# Accepted input length before normalising; a code is 19 characters with dashes.
MAX_INPUT_LENGTH = 64

# Compared against when no code is stored, so that case takes the same path.
_NO_CODE = hashlib.sha256(b"no setup code stored").hexdigest()


def generate() -> str:
    """A fresh code, e.g. ``ABCD-EFGH-JKMN-PQRS``."""
    groups = ("".join(secrets.choice(ALPHABET) for _ in range(GROUP_LENGTH)) for _ in range(GROUPS))
    return "-".join(groups)


def normalize(code: str) -> str:
    """Upper case, without dashes and whitespace."""
    return "".join(ch for ch in code.upper() if ch != "-" and not ch.isspace())


def digest(code: str) -> str:
    return hashlib.sha256(normalize(code).encode()).hexdigest()


def store(db: Session, code: str) -> None:
    """Replace any stored code with ``code`` (its hash). Doesn't commit."""
    value: dict[str, Any] = {"sha256": digest(code), "created_at": utcnow().isoformat()}
    row = db.get(Setting, SETUP_CODE_KEY)
    if row is None:
        db.add(Setting(key=SETUP_CODE_KEY, value=value))
    else:
        row.value = value


def matches(db: Session, submitted: str) -> bool:
    """True if ``submitted`` is the stored code. False when none is stored."""
    row = db.get(Setting, SETUP_CODE_KEY)
    stored = row.value.get("sha256") if row is not None and isinstance(row.value, dict) else None
    ok = isinstance(stored, str) and normalize(submitted) != ""
    expected = stored if isinstance(stored, str) else _NO_CODE
    return hmac.compare_digest(digest(submitted).encode(), expected.encode()) and ok


def clear(db: Session) -> None:
    """Delete the stored code, if any. Doesn't commit."""
    db.execute(delete(Setting).where(Setting.key == SETUP_CODE_KEY))
