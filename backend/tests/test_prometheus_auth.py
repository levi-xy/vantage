"""Optional auth from the backend to Prometheus (Settings -> Prometheus).

Security assertions: the password or token is never in a response, audit entry or log
line; a stored secret that can't be read stops all queries instead of sending a
half-configured request; rotation and restore treat the secret like every other one.
"""

import base64
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.api.deps import CSRF_HEADER
from app.core import key_check
from app.core.crypto import SecretBox
from app.core.db import make_session_factory
from app.models import AuditLog, Role, Setting
from app.query import auth as pa
from app.query.proxy import PrometheusClient, QueryError, QueryRequest
from tests.conftest import TEST_MASTER_KEY
from tests.test_auth import add_user, do_setup, login, make_client
from tests.test_key_check import OTHER_KEY, add_instance, run_cli, seal_with

API = "/api/v1/settings/prometheus"
URL = "http://prometheus.internal.example:9090"
# Fake values, derived at runtime so no credential-looking literal sits in the repo.
USER = "example-user"
PASSWORD = "example-pass-" + "x" * 8
TOKEN = "example-token-" + "y" * 8
SECRETS = (PASSWORD, TOKEN)


class FakePrometheus:
    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(dict(request.headers))
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        body = {"status": "success", "data": {"resultType": "scalar", "result": [1, "1"]}}
        return httpx.Response(200, json=body)


@pytest.fixture
def prom() -> FakePrometheus:
    return FakePrometheus()


@pytest.fixture
def admin(engine: Engine, db_url: str, prom: FakePrometheus) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        state = c.app.state  # type: ignore[attr-defined]

        def client() -> PrometheusClient:
            http = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(prom))
            return PrometheusClient(http, auth=state.prometheus_auth)  # the shared holder

        state.prometheus, state.alert_prometheus = client(), client()
        c.headers[CSRF_HEADER] = do_setup(c)["csrf_token"]
        yield c


def put(c: TestClient, **auth: object) -> httpx.Response:
    return c.put(API, json={"auth": auth})


def probe(c: TestClient, which: str = "prometheus") -> None:
    import asyncio

    client: PrometheusClient = getattr(c.app.state, which)  # type: ignore[attr-defined]
    asyncio.run(client.query(QueryRequest(query="vector(1)")))


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def audit_text(engine: Engine) -> str:
    with make_session_factory(engine)() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.target_id == "prometheus_auth")).all()
        return repr([r.changes for r in rows])


def test_get_put_and_never_a_secret(
    admin: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert admin.get(API).json() == {
        "url": admin.app.state.settings.prometheus_url,  # type: ignore[attr-defined]
        "mode": "bundled",
        "auth": {"kind": "none", "username": None, "secret_set": False, "unreadable": False},
    }
    assert put(admin, kind="basic", secret=PASSWORD).status_code == 422  # no user name
    assert put(admin, kind="bearer").status_code == 422  # no secret stored yet
    assert put(admin, kind="bearer", secret="two words").status_code == 422
    assert put(admin, kind="bearer", secret="bad\r\nX-Evil: 1").status_code == 422

    bodies = [put(admin, kind="basic", username=USER, secret=PASSWORD)]
    assert bodies[0].json()["auth"] == {
        "kind": "basic",
        "username": USER,
        "secret_set": True,
        "unreadable": False,
    }
    # An omitted secret keeps the stored one (the user name may change).
    bodies.append(put(admin, kind="basic", username="other-user"))
    assert bodies[1].json()["auth"]["username"] == "other-user"
    # A secret of another kind is never reused.
    assert put(admin, kind="bearer").status_code == 422
    bodies += [put(admin, kind="bearer", secret=TOKEN), admin.get(API)]
    bodies.append(put(admin, kind="bearer", secret=""))  # refused
    assert bodies[-1].status_code == 422

    for r in bodies:
        assert not any(s in r.text for s in SECRETS)
    assert not any(s in caplog.text for s in SECRETS)
    text = audit_text(engine)
    assert not any(s in text for s in SECRETS) and "<changed>" in text
    with make_session_factory(engine)() as db:
        entry = db.scalars(select(AuditLog).where(AuditLog.target_id == "prometheus_auth")).first()
        assert entry is not None
        assert (entry.action, entry.target_type) == ("update", "settings")
        assert entry.changes == {"kind": "basic", "username": USER, "secret": "<changed>"}
        raw = db.get(Setting, pa.SETTING_KEY)
        assert raw is not None and not any(s in repr(raw.value) for s in SECRETS)

    assert put(admin, kind="none").json()["auth"] == {
        "kind": "none",
        "username": None,
        "secret_set": False,
        "unreadable": False,
    }


def test_headers_for_both_clients_and_no_restart(admin: TestClient, prom: FakePrometheus) -> None:
    for which in ("prometheus", "alert_prometheus"):
        probe(admin, which)
    assert all("authorization" not in h for h in prom.seen)

    put(admin, kind="basic", username=USER, secret=PASSWORD)
    for which in ("prometheus", "alert_prometheus"):
        probe(admin, which)
    assert prom.seen[-1]["authorization"] == basic(USER, PASSWORD)
    assert prom.seen[-2]["authorization"] == basic(USER, PASSWORD)

    put(admin, kind="bearer", secret=TOKEN)
    probe(admin)
    assert prom.seen[-1]["authorization"] == f"Bearer {TOKEN}"

    put(admin, kind="none")
    probe(admin)
    assert "authorization" not in prom.seen[-1]


def test_401_is_auth_failed_and_the_test_button_says_so(
    admin: TestClient, prom: FakePrometheus
) -> None:
    assert admin.post(API + "/test").json() == {"ok": True, "reason": None}
    prom.status = 401
    with pytest.raises(QueryError) as caught:
        probe(admin)
    assert caught.value.unreachable == "auth_failed"
    assert admin.post(API + "/test").json() == {"ok": False, "reason": "auth_failed"}
    prom.status = 403
    assert admin.post(API + "/test").json() == {"ok": False, "reason": "auth_failed"}


def test_viewers_are_refused(admin: TestClient, engine: Engine) -> None:
    with make_session_factory(engine)() as db:
        add_user(db, "viewer1", Role.VIEWER)
    viewer = TestClient(admin.app, base_url="https://testserver")  # type: ignore[arg-type]
    viewer.headers[CSRF_HEADER] = login(viewer, "viewer1")["csrf_token"]
    assert viewer.get(API).status_code == 403
    assert put(viewer, kind="none").status_code == 403
    assert viewer.post(API + "/test").status_code == 403


def test_key_mismatch_refuses_the_put_and_unreadable_credentials_stop_queries(
    admin: TestClient, engine: Engine, prom: FakePrometheus
) -> None:
    put(admin, kind="bearer", secret=TOKEN)
    # The master key stops matching the stored secrets.
    with make_session_factory(engine)() as db:
        add_instance(db, OTHER_KEY)
        row = db.get(Setting, key_check.KEY_CHECK_KEY)
        assert row is not None
        row.value = key_check.seal(SecretBox(OTHER_KEY))
        db.commit()
    r = put(admin, kind="bearer", secret=TOKEN + "2")
    assert r.status_code == 503 and r.json()["reason"] == "key_mismatch"
    assert TOKEN not in r.text

    # The credentials stored under another key can't be read: nothing is sent.
    with make_session_factory(engine)() as db:
        row = db.get(Setting, pa.SETTING_KEY)
        assert row is not None
        blob = pa.seal_secret(SecretBox(OTHER_KEY), TOKEN)
        row.value = pa.row_value("bearer", None, blob)
        db.commit()
        admin.app.state.prometheus_auth.load(db, SecretBox(TEST_MASTER_KEY))  # type: ignore[attr-defined]
    sent = len(prom.seen)
    for which in ("prometheus", "alert_prometheus"):
        with pytest.raises(QueryError) as caught:
            probe(admin, which)
        assert caught.value.unreachable == "credentials_unreadable"
        assert TOKEN not in caught.value.message
    assert admin.post(API + "/test").json() == {"ok": False, "reason": "credentials_unreadable"}
    assert len(prom.seen) == sent

    # Clearing is the way out, and it works without a matching key.
    assert put(admin, kind="none").status_code == 200
    probe(admin)
    assert "authorization" not in prom.seen[-1]


def test_rotation_re_encrypts_the_secret(engine: Engine, db_url: str, tmp_path: Path) -> None:
    with make_session_factory(engine)() as db:
        blob = pa.seal_secret(SecretBox(TEST_MASTER_KEY), TOKEN)
        db.add(Setting(key=pa.SETTING_KEY, value=pa.row_value("bearer", None, blob)))
        seal_with(db, TEST_MASTER_KEY)
        db.commit()
    key_file = tmp_path / "master.key.new"
    key_file.write_text(base64.b64encode(OTHER_KEY).decode())
    assert run_cli(db_url, "rotate-master-key", "--new-key-file", str(key_file)) == 0
    with make_session_factory(engine)() as db:
        row = db.get(Setting, pa.SETTING_KEY)
        assert row is not None
        stored = pa.parse_row(row.value)
        assert stored.blob is not None and stored.kind == "bearer"
        assert pa.open_secret(SecretBox(OTHER_KEY), stored.blob) == TOKEN
        with pytest.raises(pa.CredentialsUnreadable):
            pa.open_secret(SecretBox(TEST_MASTER_KEY), stored.blob)
        assert key_check.check(db, SecretBox(OTHER_KEY)).ok


def test_restore_counts_the_secret(engine: Engine, db_url: str, tmp_path: Path) -> None:
    # An instance on the current key plus Prometheus credentials on another one: a
    # mixed-key backup, which restore refuses. Without counting the settings row it
    # would pass.
    with make_session_factory(engine)() as db:
        add_instance(db, TEST_MASTER_KEY)
        seal_with(db, TEST_MASTER_KEY)
        blob = pa.seal_secret(SecretBox(OTHER_KEY), TOKEN)
        db.add(Setting(key=pa.SETTING_KEY, value=pa.row_value("bearer", None, blob)))
        db.commit()
    target = tmp_path / "b.json.gz"
    assert run_cli(db_url, "backup", str(target)) == 0
    assert run_cli(db_url, "restore", "--overwrite", str(target)) == 1

    # With a consistent key the row is part of the backup and comes back.
    with make_session_factory(engine)() as db:
        db.merge(
            Setting(
                key=pa.SETTING_KEY,
                value=pa.row_value(
                    "bearer", None, pa.seal_secret(SecretBox(TEST_MASTER_KEY), TOKEN)
                ),
            )
        )
        db.commit()
    target = tmp_path / "good.json.gz"
    assert run_cli(db_url, "backup", str(target)) == 0
    with make_session_factory(engine)() as db:
        db.delete(db.get(Setting, pa.SETTING_KEY))
        db.commit()
    assert run_cli(db_url, "restore", "--overwrite", str(target)) == 0
    with make_session_factory(engine)() as db:
        row = db.get(Setting, pa.SETTING_KEY)
        assert row is not None
        stored = pa.parse_row(row.value)
        assert stored.blob is not None
        assert pa.open_secret(SecretBox(TEST_MASTER_KEY), stored.blob) == TOKEN


def test_unreadable_stored_row_is_reported_and_not_reused(
    admin: TestClient, engine: Engine
) -> None:
    # A secret sealed with another key while the key check still passes.
    with make_session_factory(engine)() as db:
        blob = pa.seal_secret(SecretBox(OTHER_KEY), PASSWORD)
        db.add(Setting(key=pa.SETTING_KEY, value=pa.row_value("basic", USER, blob)))
        db.commit()
    got = admin.get(API).json()["auth"]
    assert got == {"kind": "basic", "username": USER, "secret_set": True, "unreadable": True}
    # Keeping the old secret isn't possible: 422, not a 500.
    r = put(admin, kind="basic", username="other-user")
    assert r.status_code == 422 and "re-enter" in r.text
    fresh = put(admin, kind="basic", username=USER, secret=PASSWORD)
    assert fresh.json()["auth"]["unreadable"] is False

    # A malformed row reads as unreadable too, and saving None recovers.
    with make_session_factory(engine)() as db:
        row = db.get(Setting, pa.SETTING_KEY)
        assert row is not None
        row.value = {"kind": "mystery"}
        db.commit()
    got = admin.get(API).json()["auth"]
    assert got["unreadable"] is True and got["kind"] == "none"
    assert put(admin, kind="none").json()["auth"]["unreadable"] is False
