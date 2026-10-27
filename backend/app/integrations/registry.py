"""The list of integrations Vantage knows about.

An integration is available only by being listed in :data:`BUILTIN_INTEGRATIONS`.
Nothing is ever imported by a name taken from user input or configuration.
Every listed class is checked by ``tests/integrations/test_conformance.py``.
"""

import copy
import re
from collections.abc import Iterable, Iterator
from typing import Any, TypedDict

from app.integrations.base import Category, Integration
from app.integrations.schema import config_json_schema

# Built-in integrations, one per line. M2.5 adds the first ones.
BUILTIN_INTEGRATIONS: tuple[type[Integration], ...] = ()

# Lowercase slug that can sit inside a Prometheus metric name: vantage_<type_id>_<metric>.
TYPE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")


class IntegrationInfo(TypedDict):
    """One entry of ``GET /api/v1/integrations``."""

    type_id: str
    name: str
    category: Category
    # The config's JSON schema; secret fields are flagged writeOnly and carry no values.
    schema: dict[str, Any]


class RegistryError(Exception):
    """The integration list is invalid (duplicate or malformed type_id)."""


class Registry:
    """Integration classes by ``type_id``, with their public descriptions."""

    def __init__(self, integrations: Iterable[type[Integration]]) -> None:
        self._by_id: dict[str, type[Integration]] = {}
        for cls in integrations:
            if not (isinstance(cls, type) and issubclass(cls, Integration)):
                raise RegistryError(f"{cls!r} is not an Integration subclass")
            type_id = getattr(cls, "type_id", None)
            if not isinstance(type_id, str) or not TYPE_ID_PATTERN.fullmatch(type_id):
                raise RegistryError(f"{cls.__name__} has an invalid type_id")
            if type_id in self._by_id:
                raise RegistryError(f"duplicate type_id {type_id!r}")
            self._by_id[type_id] = cls
        self._info: list[IntegrationInfo] | None = None

    def __iter__(self) -> Iterator[type[Integration]]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, type_id: str) -> type[Integration] | None:
        """The class for ``type_id``, or ``None`` if no such integration is registered."""
        return self._by_id.get(type_id)

    def info(self) -> list[IntegrationInfo]:
        """Public descriptions, sorted by ``type_id``. Schemas are static, so built once."""
        if self._info is None:
            self._info = [
                IntegrationInfo(
                    type_id=cls.type_id,
                    name=cls.display_name,
                    category=cls.category,
                    schema=config_json_schema(cls.config_schema),
                )
                for _, cls in sorted(self._by_id.items())
            ]
        return copy.deepcopy(self._info)  # callers can't alter the cached schemas


def builtin_registry() -> Registry:
    return Registry(BUILTIN_INTEGRATIONS)
