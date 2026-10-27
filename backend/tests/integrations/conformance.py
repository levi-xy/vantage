"""Checks every registered integration must pass (run by ``test_conformance.py``).

Each check returns a list of problems; an empty list means the integration conforms.
Keeping them as plain functions lets ``test_conformance.py`` prove each one catches
the broken fakes in ``fake.py``.
"""

import inspect
import json
import re
import types
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, SecretBytes, SecretStr

from app.core.net_guard import NetGuard
from app.integrations.base import CardSpec, Category, Integration
from app.integrations.registry import TYPE_ID_PATTERN
from app.integrations.schema import config_json_schema

CATEGORIES = frozenset(get_args(Category))
CARD_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
# Field names that clearly hold a credential and therefore must be SecretStr.
SECRET_NAME_PATTERN = re.compile(
    r"(^|_)(password|passwd|passphrase|secret|token|community|api_?key|private_key|"
    r"auth_key|priv_key)$"
)
# Pydantic's mask for secret values; even that must not be served.
MASK = "**********"


def check_class_vars(cls: type[Integration]) -> list[str]:
    problems: list[str] = []
    type_id = getattr(cls, "type_id", None)
    if not isinstance(type_id, str) or not TYPE_ID_PATTERN.fullmatch(type_id):
        problems.append(f"type_id {type_id!r} is not a lowercase slug ({TYPE_ID_PATTERN.pattern})")
    for name in ("display_name", "version"):
        value = getattr(cls, name, None)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{name} must be a non-empty string")
    category = getattr(cls, "category", None)
    if category not in CATEGORIES:
        problems.append(f"category {category!r} is not one of {sorted(CATEGORIES)}")
    schema = getattr(cls, "config_schema", None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        problems.append("config_schema must be a pydantic BaseModel subclass")
    hosts = getattr(cls, "cloud_hosts", None)
    if not isinstance(hosts, tuple) or not all(isinstance(h, str) for h in hosts):
        problems.append("cloud_hosts must be a tuple of host names")
    else:
        try:
            NetGuard(cloud_hosts=hosts)
        except ValueError as exc:
            problems.append(f"cloud_hosts rejected by net_guard: {exc}")
    if inspect.isabstract(cls):
        problems.append(f"abstract methods not implemented: {sorted(cls.__abstractmethods__)}")
    return problems


def _contains(annotation: Any, target: type) -> bool:
    if annotation is target:
        return True
    return any(_contains(arg, target) for arg in get_args(annotation))


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in get_args(annotation) for m in _nested_models(arg)]


def _is_plain_secret(annotation: Any) -> bool:
    """``SecretStr`` or ``SecretStr | None``: the only shapes the config/secrets split handles."""
    if annotation is SecretStr:
        return True
    if get_origin(annotation) in (Union, types.UnionType):
        return set(get_args(annotation)) == {SecretStr, type(None)}
    return False


def secret_field_names(schema: type[BaseModel]) -> list[str]:
    """Top-level fields typed ``SecretStr`` (or ``SecretStr | None``), by their JSON name."""
    return [
        info.alias or name
        for name, info in schema.model_fields.items()
        if _is_plain_secret(info.annotation)
    ]


def check_secret_fields(cls: type[Integration]) -> list[str]:
    """Every secret is a top-level ``SecretStr``; nothing credential-like is a plain type."""
    problems: list[str] = []
    seen: set[type[BaseModel]] = set()

    def walk(model: type[BaseModel], path: str, top: bool) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            ann = info.annotation
            where = f"{path}{name}"
            secret = _contains(ann, SecretStr) or _contains(ann, SecretBytes)
            if _contains(ann, SecretBytes):
                problems.append(f"{where}: secrets must be SecretStr, not SecretBytes")
            elif secret and not top:
                problems.append(f"{where}: secrets must be top-level fields, not nested")
            elif secret and not _is_plain_secret(ann):
                problems.append(f"{where}: secrets must be SecretStr or SecretStr | None")
            if not secret and SECRET_NAME_PATTERN.search(name.lower()):
                problems.append(f"{where}: looks like a credential but is not SecretStr")
            for nested in _nested_models(ann):
                walk(nested, f"{where}.", top=False)

    schema = getattr(cls, "config_schema", None)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        walk(schema, "", top=True)
    return problems


def secret_defaults(schema: type[BaseModel]) -> list[str]:
    """Secret values declared in the schema itself (defaults and examples)."""
    values: list[str] = []
    for info in schema.model_fields.values():
        if not _contains(info.annotation, SecretStr):
            continue
        if isinstance(info.default, SecretStr):
            values.append(info.default.get_secret_value())
        for example in info.examples or ():
            values.append(
                example.get_secret_value() if isinstance(example, SecretStr) else str(example)
            )
    return [v for v in values if v]


def check_served_schema(cls: type[Integration], served: dict[str, Any]) -> list[str]:
    """``served`` is the schema ``GET /integrations`` returned for ``cls``."""
    problems: list[str] = []
    schema = cls.config_schema
    properties = served.get("properties", {})
    secrets = set(secret_field_names(schema))
    for name in secrets:
        prop = properties.get(name)
        if not isinstance(prop, dict) or prop.get("writeOnly") is not True:
            problems.append(f"secret field {name!r} is not flagged writeOnly")
        elif "default" in prop or "examples" in prop:
            problems.append(f"secret field {name!r} carries a default or examples")
    for name, prop in properties.items():
        if name not in secrets and isinstance(prop, dict) and prop.get("writeOnly"):
            problems.append(f"non-secret field {name!r} is flagged writeOnly")
    text = json.dumps(served)
    if MASK in text:
        problems.append("schema contains a masked secret value")
    for value in secret_defaults(schema):
        if value in text:
            problems.append("schema contains a secret default or example value")
    if served != config_json_schema(schema):
        problems.append("served schema differs from config_json_schema(config_schema)")
    return problems


def check_runtime(cls: type[Integration]) -> list[str]:
    """``cards()`` and ``poll_interval_seconds()`` of an instance of ``cls``."""
    problems: list[str] = []
    integration = cls()
    cards = integration.cards()
    if not isinstance(cards, list):
        return ["cards() must return a list"]
    for card in cards:
        if not isinstance(card, CardSpec):
            problems.append(f"cards() returned {type(card).__name__}, not CardSpec")
            continue
        if not CARD_TYPE_PATTERN.fullmatch(card.card_type):
            problems.append(f"card_type {card.card_type!r} is not a folder-name slug")
        if not card.title.strip():
            problems.append(f"card {card.card_type!r} has an empty title")
        if any(n < 1 for n in card.default_size):
            problems.append(f"card {card.card_type!r} default_size must be positive")
    interval = integration.poll_interval_seconds()
    if not isinstance(interval, int) or interval < 1:
        problems.append("poll_interval_seconds() must be a positive int")
    return problems
