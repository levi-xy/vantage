"""This install's identity (lite-install work, PR A).

The ``settings`` row ``install_id`` = ``{"id": "<uuid4>"}`` is made once, by migration 0015
or, failing that, by :func:`ensure` at startup, and never changes. Backups carry it, so a
disaster recovery onto a new server keeps matching the history in Prometheus.

Every series Vantage exposes (``/metrics``, ``/sd/targets``) carries ``vantage_install=<id>``
and every query it sends is scoped to it (``app.query.scope``), so several installs, or
other users, can share one Prometheus without reading each other's data.

The backend loads the id once at startup into this module (like ``app.core.timezone``);
the collector, SD, ``/metrics`` and the query client read it from here.
"""

import logging
import uuid
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import Setting

log = logging.getLogger(__name__)

SETTING_KEY = "install_id"
LABEL = "vantage_install"

# How Vantage's Prometheus is run: ``bundled`` = the compose stack's own (it holds only
# this install's data, and series stored before the label existed have none);
# ``external`` = the user's, possibly shared.
PrometheusMode = Literal["bundled", "external"]
MODES: tuple[PrometheusMode, ...] = ("bundled", "external")

_id: str | None = None
_mode: PrometheusMode = "bundled"


def valid(value: object) -> str | None:
    """The id in a stored ``settings`` value, or None when it isn't ``{"id": <uuid>}``."""
    raw = value.get("id") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == raw else None


def new_value() -> dict[str, Any]:
    return {"id": str(uuid.uuid4())}


def ensure(db: Session) -> str:
    """The stored install id; made (and committed) when missing or unreadable."""
    row = db.get(Setting, SETTING_KEY)
    found = valid(row.value) if row is not None else None
    if found is not None:
        return found
    value = new_value()
    if row is None:
        db.add(Setting(key=SETTING_KEY, value=value))
    else:
        log.warning("the stored install id is unreadable; made a new one")
        row.value = value
    db.commit()
    return str(value["id"])


def configure(install_id: str, mode: PrometheusMode = "bundled") -> None:
    global _id, _mode
    if valid({"id": install_id}) is None:
        raise ValueError("not an install id")
    _id, _mode = install_id, mode


def load(db: Session, mode: PrometheusMode = "bundled") -> str:
    install_id = ensure(db)
    configure(install_id, mode)
    return install_id


def current_id() -> str | None:
    return _id


def current_mode() -> PrometheusMode:
    return _mode
