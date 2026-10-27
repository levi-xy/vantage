"""Test-only integrations. Never listed in the production registry.

``FakeIntegration`` is a well-formed integration that exercises the conformance suite
(secret fields required, optional and with a default). The ``Broken*`` classes each
break one rule, to prove the suite catches it.
"""

from typing import ClassVar

from pydantic import BaseModel, Field, SecretBytes, SecretStr

from app.integrations.base import (
    CardSpec,
    Category,
    FirewallHint,
    InstanceContext,
    Integration,
    Sample,
    TestResult,
)

# Obviously fake values; the conformance suite proves none of them is ever served.
FAKE_DEFAULT_SECRET = "example-default-secret-5a4b"
FAKE_EXAMPLE_SECRET = "example-example-secret-9c8d"
FAKE_SECRET_VALUES = (FAKE_DEFAULT_SECRET, FAKE_EXAMPLE_SECRET)


class FakeConfig(BaseModel):
    host: str = Field(examples=["192.0.2.10"])
    port: int = 443
    verify_tls: bool = True
    api_token: SecretStr
    snmp_community: SecretStr | None = None
    client_secret: SecretStr = Field(
        default=SecretStr(FAKE_DEFAULT_SECRET), examples=[FAKE_EXAMPLE_SECRET]
    )


class FakeIntegration(Integration):
    type_id = "fake_device"
    display_name = "Fake device"
    category: ClassVar[Category] = "generic"
    version = "0.1.0"
    config_schema = FakeConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        return TestResult(ok=True, message="reachable")

    async def poll(self, ctx: InstanceContext) -> list[Sample]:
        return [Sample(name="vantage_fake_device_up", value=1, type="gauge")]

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="generic-status", title="Status", default_size=(2, 2))]

    def firewall_hints(self, ctx: InstanceContext) -> list[FirewallHint]:
        config = ctx.config_as(FakeConfig)
        return [
            FirewallHint(protocol="tcp", port=config.port, destination=config.host, reason="API")
        ]


class FakeCloudIntegration(FakeIntegration):
    type_id = "fake_cloud"
    display_name = "Fake cloud"
    category: ClassVar[Category] = "cloud"
    cloud_hosts = ("api.example.com",)


# --- broken integrations, one rule each ------------------------------------------------


class BrokenClassVars(FakeIntegration):
    type_id = "Bad-ID"
    display_name = ""
    category = "router"  # type: ignore[assignment]
    version = ""
    cloud_hosts = ("192.0.2.1",)  # addresses aren't allowed, only host names


class BrokenNotAModel(FakeIntegration):
    type_id = "broken_schema"
    config_schema = dict  # type: ignore[assignment]


class PlainPasswordConfig(BaseModel):
    host: str
    password: str  # should be SecretStr


class BrokenPlainPassword(FakeIntegration):
    type_id = "broken_password"
    config_schema = PlainPasswordConfig


class Credentials(BaseModel):
    username: str
    api_key: SecretStr


class NestedSecretConfig(BaseModel):
    host: str
    credentials: Credentials  # a nested secret would land in the plaintext config


class BrokenNestedSecret(FakeIntegration):
    type_id = "broken_nested"
    config_schema = NestedSecretConfig


class SecretBytesConfig(BaseModel):
    host: str
    key: SecretBytes


class BrokenSecretBytes(FakeIntegration):
    type_id = "broken_bytes"
    config_schema = SecretBytesConfig


class SecretListConfig(BaseModel):
    host: str
    tokens: list[SecretStr]


class BrokenSecretList(FakeIntegration):
    type_id = "broken_list"
    config_schema = SecretListConfig


class BrokenCards(FakeIntegration):
    type_id = "broken_cards"

    def cards(self) -> list[CardSpec]:
        return [CardSpec(card_type="Not A Folder", title="", default_size=(0, 2))]

    def poll_interval_seconds(self) -> int:
        return 0


class BrokenAbstract(Integration):
    """Forgets to implement ``cards`` and ``firewall_hints``."""

    type_id = "broken_abstract"
    display_name = "Broken"
    category: ClassVar[Category] = "generic"
    version = "0.1.0"
    config_schema = FakeConfig

    async def test_connection(self, ctx: InstanceContext) -> TestResult:
        return TestResult(ok=True, message="reachable")
