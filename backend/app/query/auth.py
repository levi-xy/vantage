"""Optional auth from the backend to Prometheus: none (default), basic or bearer.

Stored in the ``settings`` row ``prometheus_auth`` = ``{"kind", "username", "secrets"}``;
``secrets`` is the base64 of a :class:`SecretBox` blob (the password or token) bound to
``row_context("settings", "prometheus_auth")``. One :class:`PrometheusAuth` holder is
shared by every ``PrometheusClient`` (the browser-query client and the evaluator's), so
a saved change applies to the next request without a restart.

The header is built once, when the settings are loaded, and never logged or returned.
If a stored secret can't be decrypted, the holder refuses to give any header
(:class:`CredentialsUnreadable`): a request is never sent half-configured.
"""

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from app.core.crypto import DecryptionError, SecretBox, row_context
from app.models import Setting

log = logging.getLogger(__name__)

SETTING_KEY = "prometheus_auth"
CONTEXT = row_context("settings", SETTING_KEY)
AuthKind = Literal["none", "basic", "bearer"]
KINDS: tuple[str, ...] = ("none", "basic", "bearer")
_SECRET_FIELD = "secret"  # noqa: S105  # nosec B105 - a field name


class CredentialsUnreadable(Exception):
    """The stored Prometheus credentials can't be read (wrong master key, damaged row)."""


@dataclass(frozen=True)
class StoredAuth:
    kind: AuthKind
    username: str | None
    blob: bytes | None  # the encrypted secret

    @property
    def secret_set(self) -> bool:
        return self.blob is not None


def parse_row(value: Any) -> StoredAuth:
    """The stored row's value; anything malformed reads as unreadable credentials."""
    if not isinstance(value, dict):
        raise CredentialsUnreadable()
    kind = value.get("kind")
    username = value.get("username")
    raw = value.get("secrets")
    if kind not in KINDS or not (username is None or isinstance(username, str)):
        raise CredentialsUnreadable()
    blob: bytes | None = None
    if raw is not None:
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise CredentialsUnreadable() from None
    return StoredAuth(cast(AuthKind, kind), username, blob)


def stored_blob(value: Any) -> bytes | None:
    """The encrypted blob of a stored row value, None when there is none or it is
    malformed (backup, restore and key rotation count it as a stored secret)."""
    try:
        return parse_row(value).blob
    except CredentialsUnreadable:
        return None


def row_value(kind: AuthKind, username: str | None, blob: bytes | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "username": username,
        "secrets": base64.b64encode(blob).decode("ascii") if blob is not None else None,
    }


def seal_secret(box: SecretBox, secret: str) -> bytes:
    return box.encrypt_secrets({_SECRET_FIELD: secret}, context=CONTEXT)


def open_secret(box: SecretBox, blob: bytes) -> str:
    try:
        return box.decrypt_secrets(blob, context=CONTEXT)[_SECRET_FIELD]
    except (DecryptionError, KeyError):
        raise CredentialsUnreadable() from None


def build_headers(stored: StoredAuth, box: SecretBox) -> dict[str, str]:
    """The request headers for ``stored``; raises :class:`CredentialsUnreadable`."""
    if stored.kind == "none":
        return {}
    if stored.blob is None:
        raise CredentialsUnreadable()
    secret = open_secret(box, stored.blob)
    if stored.kind == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    pair = f"{stored.username or ''}:{secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}


class PrometheusAuth:
    """The current headers, shared by every client. Never prints them."""

    def __init__(self) -> None:
        self._headers: dict[str, str] = {}
        self._unreadable = False

    def __repr__(self) -> str:
        return "PrometheusAuth(<redacted>)"

    def headers(self) -> dict[str, str]:
        if self._unreadable:
            raise CredentialsUnreadable()
        return dict(self._headers)

    def set(self, headers: dict[str, str]) -> None:
        self._headers = headers
        self._unreadable = False

    def load(self, db: Session, box: SecretBox) -> None:
        """Read the row and apply it. An unreadable row (or database) fails closed."""
        try:
            row = db.get(Setting, SETTING_KEY)
            self.set({} if row is None else build_headers(parse_row(row.value), box))
        except CredentialsUnreadable:
            log.error("the stored Prometheus credentials can't be read; queries are paused")
            self._headers, self._unreadable = {}, True
        except Exception as exc:  # a database error: don't guess, don't send unauthenticated
            log.error("Prometheus credentials could not be loaded (%s)", type(exc).__name__)
            self._headers, self._unreadable = {}, True
