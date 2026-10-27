from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.integrations.base import Integration
from app.integrations.registry import BUILTIN_INTEGRATIONS, Registry
from tests.integrations.fake import FakeCloudIntegration, FakeIntegration
from tests.test_auth import do_setup, make_client

# Every production integration, plus test-only fakes so the suite always runs something.
# New integrations get the conformance checks by being listed in registry.py.
CONFORMANCE_SUBJECTS: list[type[Integration]] = [
    *BUILTIN_INTEGRATIONS,
    FakeIntegration,
    FakeCloudIntegration,
]


@pytest.fixture(params=CONFORMANCE_SUBJECTS, ids=lambda cls: cls.type_id)
def integration_cls(request: pytest.FixtureRequest) -> type[Integration]:
    cls: type[Integration] = request.param
    return cls


ClientFactory = Callable[[Registry | None], TestClient]


@pytest.fixture
def client_for(engine: Engine, db_url: str) -> Iterator[ClientFactory]:
    """Builds a logged-in admin client whose app uses the given registry (None: built-in)."""
    clients: list[TestClient] = []

    def build(registry: Registry | None) -> TestClient:
        client = make_client(db_url, registry=registry)
        client.__enter__()
        clients.append(client)
        do_setup(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)
