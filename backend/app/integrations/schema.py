"""JSON schema of an integration's config, as served by ``GET /integrations``.

Pydantic already marks ``SecretStr`` as ``writeOnly``, but it also emits a masked
default and any ``examples`` for it, and for ``SecretStr | None`` it puts the flag on
an ``anyOf`` branch rather than on the property. This module fixes all three, so the
frontend can rely on a property-level ``writeOnly`` and no secret-derived value
(default, example, mask) is ever served.
"""

from typing import Any

from pydantic import BaseModel

_JsonObj = dict[str, Any]


def config_json_schema(schema: type[BaseModel]) -> _JsonObj:
    """The validation-mode JSON schema of ``schema`` with every secret field sanitised."""
    result = schema.model_json_schema(mode="validation")
    _scrub_properties(result)
    if _contains_write_only(result, nested_properties=True):
        # Object-level examples/defaults would list the secret values next to the rest.
        _scrub_objects(result)
    return result


def _children(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return list(node.values())
    if isinstance(node, list):
        return node
    return []


def _scrub_properties(node: Any) -> None:
    properties = node.get("properties") if isinstance(node, dict) else None
    if isinstance(properties, dict):
        for prop in properties.values():
            if _contains_write_only(prop, nested_properties=False):
                prop["writeOnly"] = True
                _drop_values(prop)
    for child in _children(node):
        _scrub_properties(child)


def _scrub_objects(node: Any) -> None:
    if isinstance(node, dict) and "properties" in node:
        node.pop("examples", None)
        node.pop("default", None)
        for prop in node["properties"].values():
            # A nested model's default/examples are objects that may hold masked secrets.
            if isinstance(prop, dict):
                if _has_object(prop.get("default")):
                    prop.pop("default")
                if _has_object(prop.get("examples")):
                    prop.pop("examples")
    for child in _children(node):
        _scrub_objects(child)


def _has_object(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(_has_object(item) for item in value)


def _contains_write_only(node: Any, *, nested_properties: bool) -> bool:
    """Whether ``node`` is or wraps (anyOf, items, ...) a ``writeOnly`` schema.

    With ``nested_properties=False`` the search stops at nested object properties, so
    an inline object holding a secret is not itself treated as one secret field.
    """
    if isinstance(node, dict):
        if node.get("writeOnly") is True:
            return True
        return any(
            _contains_write_only(value, nested_properties=nested_properties)
            for key, value in node.items()
            if nested_properties or key != "properties"
        )
    if isinstance(node, list):
        return any(_contains_write_only(item, nested_properties=nested_properties) for item in node)
    return False


def _drop_values(node: Any) -> None:
    if isinstance(node, dict):
        for key in ("default", "examples", "example", "const", "enum"):
            node.pop(key, None)
    for child in _children(node):
        _drop_values(child)
