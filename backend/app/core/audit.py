"""Audit log: who created, changed or deleted what, and when (M1.4).

Every state-changing action calls :func:`record` in the same database transaction as the
change itself, so the entry is committed (or rolled back) together with it.

Secret values never reach the ``audit_log`` table. Before anything is stored, ``changes``
is passed through :func:`redact`, which replaces the value of every secret field with the
literal ``"<changed>"``. A field counts as secret when any of these hold:

* its normalised key (lowercase, letters and digits only, so ``API-Key``, ``apiKey`` and
  ``api_key`` are all ``apikey``) contains any of :data:`SECRET_KEY_SUBSTRINGS`
  (``secret``, ``token``, ``password``, ``key``, ``psk``, ``credential``, ...);
* its key is listed in ``secret_keys`` passed by the caller;
* its key is a ``SecretStr``/``SecretBytes`` field of a Pydantic ``schema`` passed by the
  caller (for example an integration's ``config_schema``), at any nesting level;
* its value is a ``SecretStr``/``SecretBytes`` or raw ``bytes`` (encrypted blobs).

Key matching applies at every nesting level. Over-redacting a field that merely shares a
name with a secret is the safe direction, and is accepted. Keys must be ``str`` (``bytes``
keys are decoded); anything else is refused rather than stringified.

Fail closed: changes to targets in :data:`SCHEMA_REQUIRED_TARGETS` (instances, channels)
must come with the schema that marks their secret fields, or :func:`record` raises.

Omitted keys are not recorded at all: on a PATCH an omitted secret means "unchanged", so
callers should pass only the fields that were actually sent (``exclude_unset=True``).
A secret key that is present is always recorded as ``"<changed>"``, even when its new
value is ``None`` or empty, so the log never reveals anything about a secret's value.
"""

import enum
import types
import typing
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, Union
from uuid import UUID

from pydantic import BaseModel, SecretBytes, SecretStr
from sqlalchemy.orm import Session

from app.models import AuditLog

REDACTED = "<changed>"

# A key is secret wherever it appears when its normalised form (see ``normalize_key``)
# contains any of these. Deliberately broad: "key" also hits harmless names such as
# "keyboard_layout". Over-redaction is the safe direction and is accepted.
SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "passphrase",
    "community",
    "key",
    "psk",
    "authorization",
    "credential",
)

# Target types whose changes must come with the schema that marks their secret fields.
SCHEMA_REQUIRED_TARGETS: frozenset[str] = frozenset({"instance", "channel"})

_SECRET_TYPES = (SecretStr, SecretBytes)
# Values redacted whatever their key: secrets, and raw bytes (encrypted blobs).
_REDACTED_VALUE_TYPES = (*_SECRET_TYPES, bytes, bytearray, memoryview)


class Action(enum.StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"


# --- secret detection --------------------------------------------------------------


def _annotation_is_secret(annotation: Any) -> bool:
    """True if the annotation is SecretStr/SecretBytes, possibly inside Optional/Union."""
    if isinstance(annotation, type) and issubclass(annotation, _SECRET_TYPES):
        return True
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType or origin is typing.Annotated:
        return any(_annotation_is_secret(arg) for arg in typing.get_args(annotation))
    return False


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _nested_models(arg)]


def secret_fields(schema: type[BaseModel]) -> frozenset[str]:
    """Names of the ``SecretStr``/``SecretBytes`` fields of a schema, including nested models.

    Both the field name and its alias are returned, since either may appear as a key.
    """
    found: set[str] = set()
    seen: set[type[BaseModel]] = set()

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            if _annotation_is_secret(info.annotation):
                found.add(name)
                if info.alias:
                    found.add(info.alias)
            for nested in _nested_models(info.annotation):
                walk(nested)

    walk(schema)
    return frozenset(found)


# --- redaction ---------------------------------------------------------------------


def _model_items(model: BaseModel) -> dict[str, Any]:
    # Field values as Python objects, so SecretStr values stay recognisable (model_dump
    # would turn them into masked strings first).
    return {name: getattr(model, name) for name in type(model).model_fields}


def normalize_key(key: str) -> str:
    """Lowercase with everything but letters and digits removed: ``API-Key`` -> ``apikey``."""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _is_secret_key(key: str, secret_keys: frozenset[str]) -> bool:
    norm = normalize_key(key)
    return norm in secret_keys or any(part in norm for part in SECRET_KEY_SUBSTRINGS)


def _key_text(key: object) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bytes):
        return key.decode("utf-8", errors="replace")
    # str() of an arbitrary key could dodge the secret-name checks; refuse instead.
    raise TypeError(f"audit changes need str keys, got {type(key).__name__}")


def _clean(value: Any, secret_keys: frozenset[str]) -> Any:
    if isinstance(value, _REDACTED_VALUE_TYPES):
        return REDACTED
    if isinstance(value, BaseModel):
        return _clean(_model_items(value), secret_keys)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = _key_text(key)
            out[k] = REDACTED if _is_secret_key(k, secret_keys) else _clean(item, secret_keys)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(item, secret_keys) for item in value]
    if isinstance(value, enum.Enum):
        return _clean(value.value, secret_keys)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    # Refuse rather than fall back to str(): an unknown object's repr could hold a secret.
    raise TypeError(f"audit changes can't store a value of type {type(value).__name__}")


def redact(
    changes: Mapping[str, Any] | BaseModel,
    *,
    secret_keys: Iterable[str] = (),
    schema: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """A JSON-safe copy of ``changes`` with every secret value replaced by ``"<changed>"``."""
    keys = set(secret_keys)
    if schema is not None:
        keys |= secret_fields(schema)
    if isinstance(changes, BaseModel):
        keys |= secret_fields(type(changes))
    cleaned: dict[str, Any] = _clean(changes, frozenset(normalize_key(k) for k in keys))
    return cleaned


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """``{field: {"old": ..., "new": ...}}`` for the fields that differ. Redact the result."""
    out: dict[str, Any] = {}
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old != new:
            out[key] = {"old": old, "new": new}
    return out


# --- recording ---------------------------------------------------------------------


def record(
    db: Session,
    *,
    action: Action | str,
    target_type: str,
    target_id: int | str | UUID | None,
    user_id: int | None,
    changes: Mapping[str, Any] | BaseModel | None = None,
    secret_keys: Iterable[str] = (),
    schema: type[BaseModel] | None = None,
) -> AuditLog:
    """Add an audit entry to ``db``'s current transaction. The caller commits.

    For updates, ``changes`` should be a :func:`diff` of the touched fields; any secret
    key in it, at any depth, is collapsed to ``"<changed>"`` (old and new alike).

    Raises ``ValueError`` when ``target_type`` is in :data:`SCHEMA_REQUIRED_TARGETS`,
    ``changes`` is non-empty and no ``schema`` is given (and ``changes`` isn't a model
    instance, which carries its own schema).
    """
    if (
        target_type in SCHEMA_REQUIRED_TARGETS
        and schema is None
        and changes
        and not isinstance(changes, BaseModel)
    ):
        raise ValueError(f"audit changes for {target_type!r} need the schema of its secrets")
    entry = AuditLog(
        user_id=user_id,
        action=str(action),
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        changes=redact(changes or {}, secret_keys=secret_keys, schema=schema),
    )
    db.add(entry)
    return entry
