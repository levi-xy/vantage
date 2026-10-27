"""An instance's config: validation, the config/secrets split, SSRF checks, contexts (M2.2).

Contract 2: the ``SecretStr`` fields of an integration's ``config_schema`` are stored,
encrypted, in ``instances.secrets``; every other field goes to ``instances.config``.
The conformance suite guarantees secrets are top-level ``SecretStr`` (or
``SecretStr | None``) fields; :func:`secret_field_keys` still refuses a schema with a
nested secret rather than store it in plaintext.

Host checks: before an instance is saved or tested, every host its config points at is
resolved and checked by the SSRF guard (Contract 5). A host is any string field named
``host`` or ``*_host``, and the host part of any field named ``url`` or ``*_url``.
Connections are checked again by the guard at connect time; this check exists so a
denied address is refused when the admin enters it, with a clear message.
"""

import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

from app.core.audit import secret_fields
from app.core.crypto import SecretBox, row_context
from app.core.net_guard import NetGuard, NetGuardError, NetPolicy
from app.integrations.base import InstanceContext, Integration

INSTANCES_TABLE = "instances"

# Builds the SSRF guard for one integration. Tests inject one with a fake resolver.
NetGuardFactory = Callable[[type[Integration]], NetGuard]


def default_net_guard_factory(cls: type[Integration]) -> NetGuard:
    """A guard with the default policy and the integration's declared ``cloud_hosts``.

    The policy is built per call so the host's own addresses are discovered fresh.
    """
    # TODO(maintainer): extra allowed CIDRs come from the settings table once the
    # admin-only settings API exists (see NetPolicy.extra_allowed).
    return NetGuard(NetPolicy(), cloud_hosts=cls.cloud_hosts)


class HostCheckError(Exception):
    """A configured host was refused. ``field`` names the config field; the message is
    the guard's (it names the host as configured and the reason, never a secret)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


# --- the config/secrets split ----------------------------------------------------------


def secret_field_keys(schema: type[BaseModel]) -> frozenset[str]:
    """The keys under which the schema's secret fields are stored (alias, else name)."""
    found = secret_fields(schema)
    keys: set[str] = set()
    for name, info in schema.model_fields.items():
        if name in found or (info.alias and info.alias in found):
            keys.add(info.alias or name)
    top_level = keys | set(schema.model_fields)
    if not found <= top_level:
        # A nested secret would end up in the plaintext config column.
        raise ValueError(f"{schema.__name__} has a nested secret field")
    return frozenset(keys)


def split(model: BaseModel) -> tuple[dict[str, Any], dict[str, str]]:
    """``(config, secrets)``: non-secret fields as JSON, and the set secret values.

    Unknown extra fields are dropped. A secret that is ``None`` is not stored.
    """
    schema = type(model)
    secret_keys = secret_field_keys(schema)
    config_names: set[str] = set()
    secrets: dict[str, str] = {}
    for name, info in schema.model_fields.items():
        key = info.alias or name
        if key not in secret_keys:
            config_names.add(name)
            continue
        value = getattr(model, name)
        if isinstance(value, SecretStr):
            secrets[key] = value.get_secret_value()
        elif value is not None:  # unreachable for a conforming schema
            raise ValueError(f"secret field {key!r} is not a SecretStr")
    config = model.model_dump(mode="json", by_alias=True, include=config_names)
    return config, secrets


def public_config(config: Mapping[str, Any], schema: type[BaseModel] | None) -> dict[str, Any]:
    """The stored config without any secret key, as returned by the API.

    ``config`` never holds secrets; this is a second line of defence.
    """
    hidden = secret_field_keys(schema) if schema is not None else frozenset()
    return {k: v for k, v in config.items() if k not in hidden}


def encrypt(box: SecretBox, instance_id: uuid.UUID, secrets: Mapping[str, str]) -> bytes | None:
    if not secrets:
        return None
    return box.encrypt_secrets(secrets, context=row_context(INSTANCES_TABLE, instance_id))


def decrypt(box: SecretBox, instance_id: uuid.UUID, blob: bytes | None) -> dict[str, str]:
    """Raises ``DecryptionError`` (whose message holds nothing secret) on a bad blob."""
    if blob is None:
        return {}
    return box.decrypt_secrets(blob, context=row_context(INSTANCES_TABLE, instance_id))


# --- host checks -----------------------------------------------------------------------


def _is_host_field(key: str) -> bool:
    return key == "host" or key.endswith("_host")


def _is_url_field(key: str) -> bool:
    return key == "url" or key.endswith("_url")


def configured_hosts(config: Mapping[str, Any]) -> list[tuple[str, str, int, str | None]]:
    """``(field, host, port, scheme)`` for every host the non-secret config points at."""
    default_port = config.get("port")
    port = default_port if isinstance(default_port, int) and 0 < default_port < 65536 else 443
    found: list[tuple[str, str, int, str | None]] = []
    for key, value in config.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if _is_host_field(key):
            found.append((key, value, port, None))
        elif _is_url_field(key):
            try:
                url = httpx.URL(value)
            except httpx.InvalidURL:
                raise HostCheckError(key, "invalid URL") from None
            if url.scheme not in ("http", "https") or not url.host:
                raise HostCheckError(key, "URL must be http:// or https:// with a host")
            found.append(
                (key, url.host, url.port or (443 if url.scheme == "https" else 80), url.scheme)
            )
    return found


async def check_hosts(guard: NetGuard, config: Mapping[str, Any]) -> None:
    """Resolve and check every configured host. Raises :class:`HostCheckError`.

    Fails closed: a host that can't be resolved right now is refused too.
    """
    for key, host, port, scheme in configured_hosts(config):
        try:
            cloud = guard.is_cloud_host(host)
            if cloud and scheme == "http":
                raise HostCheckError(key, f"{host} must be reached over https")
            await guard.resolve(host, port, cloud_host=cloud)
        except NetGuardError as exc:
            raise HostCheckError(key, str(exc)) from None


# --- contexts --------------------------------------------------------------------------


def build_context(
    *,
    instance_id: uuid.UUID,
    name: str,
    config: BaseModel,
    net_guard: NetGuard,
) -> InstanceContext:
    # TODO(maintainer): InstanceContext.labels is left empty here; which labels belong in
    # it (the four standard vantage_* labels, the instance's tags, or both) is still open
    # and not needed by test_connection. M2.4 builds contexts for polling.
    return InstanceContext(instance_id=instance_id, name=name, config=config, net_guard=net_guard)
