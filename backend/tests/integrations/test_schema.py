"""``config_json_schema``: what the frontend form is generated from."""

import json

from pydantic import BaseModel, Field, SecretStr

from app.integrations.schema import config_json_schema
from tests.integrations.fake import FAKE_SECRET_VALUES, FakeConfig


def test_secret_fields_flagged_write_only_at_property_level() -> None:
    props = config_json_schema(FakeConfig)["properties"]
    assert props["api_token"]["writeOnly"] is True
    assert props["api_token"]["format"] == "password"
    # SecretStr | None: pydantic puts the flag on the anyOf branch; we lift it.
    assert props["snmp_community"]["writeOnly"] is True
    assert props["client_secret"]["writeOnly"] is True
    for name in ("host", "port", "verify_tls"):
        assert "writeOnly" not in props[name]


def test_secret_defaults_examples_and_masks_are_dropped() -> None:
    schema = config_json_schema(FakeConfig)
    props = schema["properties"]
    for name in ("api_token", "snmp_community", "client_secret"):
        assert "default" not in props[name]
        assert "examples" not in props[name]
    text = json.dumps(schema)
    assert "*" not in text
    for value in FAKE_SECRET_VALUES:
        assert value not in text


def test_non_secret_hints_are_kept() -> None:
    schema = config_json_schema(FakeConfig)
    props = schema["properties"]
    assert props["host"]["examples"] == ["192.0.2.10"]
    assert props["port"]["default"] == 443
    assert schema["required"] == ["host", "api_token"]


class Nested(BaseModel):
    token: SecretStr = SecretStr("example-nested-token")


class WithNested(BaseModel):
    host: str
    inner: Nested = Nested()
    model_config = {
        "json_schema_extra": {"examples": [{"host": "10.0.0.5", "token": "example-model-token"}]}
    }


class WithModelExamples(BaseModel):
    host: str
    token: SecretStr
    model_config = {
        "json_schema_extra": {"examples": [{"host": "10.0.0.5", "token": "example-model-token"}]}
    }


def test_nested_models_are_sanitised_too() -> None:
    schema = config_json_schema(WithNested)
    token = schema["$defs"]["Nested"]["properties"]["token"]
    assert token["writeOnly"] is True
    assert "default" not in token
    assert "examples" not in schema
    assert "default" not in schema["properties"]["inner"]
    text = json.dumps(schema)
    assert "*" not in text
    assert "example-nested-token" not in text
    assert "example-model-token" not in text


def test_model_level_examples_dropped_when_model_has_secrets() -> None:
    schema = config_json_schema(WithModelExamples)
    assert "examples" not in schema
    assert "example-model-token" not in json.dumps(schema)


def test_aliased_secret_uses_its_json_name() -> None:
    class Aliased(BaseModel):
        api_key: SecretStr = Field(alias="apiKey")

    props = config_json_schema(Aliased)["properties"]
    assert props["apiKey"]["writeOnly"] is True
