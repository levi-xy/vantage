"""Shared conformance suite: runs for every integration in ``registry.py`` (plus fakes).

A new integration gets these checks just by being listed in ``BUILTIN_INTEGRATIONS``.
The second half proves each check actually catches the rule it enforces.
"""

from collections.abc import Callable

import pytest

from app.integrations.base import Integration
from app.integrations.registry import Registry
from app.integrations.schema import config_json_schema
from tests.integrations import fake
from tests.integrations.conformance import (
    MASK,
    check_class_vars,
    check_runtime,
    check_secret_fields,
    check_served_schema,
    secret_defaults,
    secret_field_names,
)
from tests.integrations.conftest import ClientFactory

# --- every registered integration ------------------------------------------------------


def test_class_vars(integration_cls: type[Integration]) -> None:
    assert check_class_vars(integration_cls) == []


def test_secret_fields_are_secret_str(integration_cls: type[Integration]) -> None:
    assert check_secret_fields(integration_cls) == []


def test_cards_and_poll_interval(integration_cls: type[Integration]) -> None:
    assert check_runtime(integration_cls) == []


def test_served_schema_flags_secrets_and_leaks_nothing(
    integration_cls: type[Integration], client_for: ClientFactory
) -> None:
    client = client_for(Registry([integration_cls]))
    r = client.get("/api/v1/integrations")
    assert r.status_code == 200
    [entry] = r.json()
    assert entry["type_id"] == integration_cls.type_id
    assert entry["name"] == integration_cls.display_name
    assert entry["category"] == integration_cls.category
    assert check_served_schema(integration_cls, entry["schema"]) == []
    # The whole body, not only the schema, is free of secret values and masks.
    assert MASK not in r.text
    for value in secret_defaults(integration_cls.config_schema):
        assert value not in r.text


# --- the checks catch what they should -------------------------------------------------


def test_the_fake_exercises_every_secret_shape() -> None:
    # Required, optional and defaulted secrets, so the suite isn't vacuous.
    assert secret_field_names(fake.FakeConfig) == ["api_token", "snmp_community", "client_secret"]
    assert set(secret_defaults(fake.FakeConfig)) == set(fake.FAKE_SECRET_VALUES)


def test_class_var_check_catches_bad_values() -> None:
    problems = " | ".join(check_class_vars(fake.BrokenClassVars))
    for expected in ("type_id", "display_name", "version", "category", "cloud_hosts"):
        assert expected in problems


def test_class_var_check_catches_non_model_schema() -> None:
    assert any("BaseModel" in p for p in check_class_vars(fake.BrokenNotAModel))


def test_class_var_check_catches_abstract_class() -> None:
    [problem] = check_class_vars(fake.BrokenAbstract)
    assert "cards" in problem and "firewall_hints" in problem


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (fake.BrokenPlainPassword, "password: looks like a credential"),
        (fake.BrokenNestedSecret, "credentials.api_key: secrets must be top-level"),
        (fake.BrokenSecretBytes, "key: secrets must be SecretStr, not SecretBytes"),
        (fake.BrokenSecretList, "tokens: secrets must be SecretStr or SecretStr | None"),
    ],
)
def test_secret_field_check_catches(cls: type[Integration], expected: str) -> None:
    assert [p for p in check_secret_fields(cls) if expected in p]


def test_runtime_check_catches_bad_cards_and_interval() -> None:
    problems = " | ".join(check_runtime(fake.BrokenCards))
    for expected in ("folder-name slug", "empty title", "default_size", "poll_interval"):
        assert expected in problems


SchemaEdit = Callable[[dict[str, object]], None]


def _unflag(schema: dict[str, object]) -> None:
    schema["properties"]["api_token"].pop("writeOnly")  # type: ignore[index]


def _leak_default(schema: dict[str, object]) -> None:
    schema["properties"]["client_secret"]["default"] = fake.FAKE_DEFAULT_SECRET  # type: ignore[index]


def _leak_mask(schema: dict[str, object]) -> None:
    schema["properties"]["host"]["description"] = MASK  # type: ignore[index]


def _flag_host(schema: dict[str, object]) -> None:
    schema["properties"]["host"]["writeOnly"] = True  # type: ignore[index]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_unflag, "not flagged writeOnly"),
        (_leak_default, "carries a default"),
        (_leak_mask, "masked secret value"),
        (_flag_host, "non-secret field 'host'"),
    ],
)
def test_served_schema_check_catches(edit: SchemaEdit, expected: str) -> None:
    schema = config_json_schema(fake.FakeConfig)
    edit(schema)
    assert [p for p in check_served_schema(fake.FakeIntegration, schema) if expected in p]
