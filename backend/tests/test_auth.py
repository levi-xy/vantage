from collections import OrderedDict
from collections.abc import Iterator
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api.auth import SETUP_MARKER
from app.api.deps import AdminDep, AuthDep
from app.core.auth import LoginRateLimiter, Passwords, csrf_token, session_id
from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.integrations.instance_config import NetGuardFactory
from app.integrations.registry import Registry
from app.main import create_app
from app.models import Role, Setting, User, UserSession

ADMIN = "admin"
PASSWORD = "example-password-123"
SECURE_COOKIE = "__Host-vantage_session"

# Cheap Argon2 parameters so the suite stays fast. Production uses argon2-cffi's defaults.
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def make_client(
    db_url: str,
    registry: Registry | None = None,
    net_guard_factory: NetGuardFactory | None = None,
    peer: str = "testclient",
    **settings: object,
) -> TestClient:
    app = create_app(
        Settings(database_url=normalize_database_url(db_url), **settings),  # type: ignore[arg-type]
        secret_box=SecretBox(b"vantage-test-key-not-a-secret-00"),
        passwords=Passwords(FAST_HASHER),
        registry=registry,
        net_guard_factory=net_guard_factory,
    )

    # Stand-ins for the admin/viewer routes later milestones add.
    @app.get("/api/v1/_test/any")
    def any_user(auth: AuthDep) -> dict[str, str]:
        return {"user": auth.user.username}

    @app.post("/api/v1/_test/admin")
    def admin_only(auth: AdminDep) -> dict[str, str]:
        return {"user": auth.user.username}

    # https so the client sends Secure cookies back, like a browser would.
    # `peer` is the TCP peer address the app sees (request.client.host).
    return TestClient(app, base_url="https://testserver", client=(peer, 50000))


@pytest.fixture
def client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    with make_client(db_url) as c:
        yield c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def do_setup(client: TestClient, username: str = ADMIN, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/v1/setup", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    body: dict[str, str] = r.json()
    return body


def add_user(db: Session, username: str, role: Role, password: str = PASSWORD) -> User:
    user = User(username=username, password_hash=FAST_HASHER.hash(password), role=role)
    db.add(user)
    db.commit()
    return user


def login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body: dict[str, str] = r.json()
    return body


def assert_no_secrets(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


# --- setup ---------------------------------------------------------------------------


def test_setup_creates_admin_and_logs_in(client: TestClient, db: Session) -> None:
    assert client.get("/api/v1/setup/status").json() == {"setup_required": True}

    me = do_setup(client)
    assert me["username"] == ADMIN
    assert me["role"] == "admin"
    assert client.get("/api/v1/me").json() == me
    assert client.get("/api/v1/setup/status").json() == {"setup_required": False}

    user = db.scalars(select(User)).one()
    assert user.role == Role.ADMIN
    assert user.password_hash.startswith("$argon2id$")
    assert user.last_login_at is not None


def test_setup_only_works_once(client: TestClient) -> None:
    do_setup(client)
    r = client.post("/api/v1/setup", json={"username": "second", "password": PASSWORD})
    assert r.status_code == 409


def test_setup_refused_when_users_exist(client: TestClient, db: Session) -> None:
    add_user(db, "existing", Role.VIEWER)
    assert client.get("/api/v1/setup/status").json() == {"setup_required": False}
    r = client.post("/api/v1/setup", json={"username": ADMIN, "password": PASSWORD})
    assert r.status_code == 409


def test_setup_stays_closed_after_all_users_are_deleted(client: TestClient, db: Session) -> None:
    do_setup(client)
    db.query(User).delete()
    db.commit()
    assert db.get(Setting, SETUP_MARKER) is not None
    assert client.get("/api/v1/setup/status").json() == {"setup_required": False}
    r = client.post("/api/v1/setup", json={"username": "intruder", "password": PASSWORD})
    assert r.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"username": ADMIN, "password": "x" * 11},  # one under the minimum
        {"username": "bad name", "password": PASSWORD},
        {"username": "", "password": PASSWORD},
        {"username": ADMIN, "password": PASSWORD, "role": "admin"},
        {"username": ADMIN, "password": "x" * 1025},
    ],
)
def test_setup_validation_never_echoes_input(client: TestClient, body: dict[str, str]) -> None:
    r = client.post("/api/v1/setup", json=body)
    assert r.status_code == 422
    assert_no_secrets(r.text, body["password"])
    assert client.get("/api/v1/setup/status").json() == {"setup_required": True}


def test_setup_requires_json(client: TestClient) -> None:
    r = client.post("/api/v1/setup", data={"username": ADMIN, "password": PASSWORD})
    assert r.status_code == 415
    assert client.get("/api/v1/setup/status").json() == {"setup_required": True}


# --- cookies -------------------------------------------------------------------------


def test_session_cookie_flags(client: TestClient) -> None:
    r = client.post("/api/v1/setup", json={"username": ADMIN, "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{SECURE_COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=43200"):
        assert flag in cookie
    assert "Domain" not in cookie


def test_insecure_cookie_option(engine: Engine, db_url: str) -> None:
    with make_client(db_url, cookie_secure=False) as client:
        r = client.post("/api/v1/setup", json={"username": ADMIN, "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("vantage_session=")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


def test_database_stores_only_a_hash_of_the_token(client: TestClient, db: Session) -> None:
    do_setup(client)
    token = client.cookies[SECURE_COOKIE]
    row = db.scalars(select(UserSession)).one()
    assert row.id == session_id(token)
    assert token not in row.id


# --- login ---------------------------------------------------------------------------


def test_login_and_me(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me = login(client, "viewer1")
    assert me["role"] == "viewer"
    assert me["csrf_token"] == csrf_token(client.cookies[SECURE_COOKIE])
    assert client.get("/api/v1/me").json() == me

    db.expire_all()
    user = db.scalars(select(User).where(User.username == "viewer1")).one()
    assert user.last_login_at is not None


@pytest.mark.parametrize("username", ["viewer1", "nobody"])
def test_bad_login_gives_one_generic_error(client: TestClient, db: Session, username: str) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    r = client.post("/api/v1/auth/login", json={"username": username, "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid username or password"}
    assert SECURE_COOKIE not in client.cookies


def test_responses_never_contain_password_or_hash(client: TestClient, db: Session) -> None:
    texts = [client.post("/api/v1/setup", json={"username": ADMIN, "password": PASSWORD}).text]
    texts.append(client.get("/api/v1/me").text)
    texts.append(
        client.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD}).text
    )
    texts.append(
        client.post("/api/v1/auth/login", json={"username": ADMIN, "password": "wrong"}).text
    )
    password_hash = db.scalars(select(User.password_hash)).one()
    for text in texts:
        assert_no_secrets(text, PASSWORD, password_hash, "argon2")


def test_login_requires_json(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    r = client.post("/api/v1/auth/login", data={"username": "viewer1", "password": PASSWORD})
    assert r.status_code == 415


def test_login_replaces_this_browsers_session(client: TestClient, db: Session) -> None:
    do_setup(client)
    first = client.cookies[SECURE_COOKIE]
    login(client, ADMIN)
    assert client.cookies[SECURE_COOKIE] != first
    assert db.scalar(select(func.count()).select_from(UserSession)) == 1


def test_login_rehashes_outdated_hash(client: TestClient, db: Session) -> None:
    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, salt_len=8)
    user = User(username="olduser", password_hash=old.hash(PASSWORD), role=Role.VIEWER)
    db.add(user)
    db.commit()
    before = user.password_hash
    login(client, "olduser")
    db.expire_all()
    after = db.get(User, user.id)
    assert after is not None and after.password_hash != before
    assert not FAST_HASHER.check_needs_rehash(after.password_hash)


# --- rate limit ----------------------------------------------------------------------


def test_login_rate_limit(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    for _ in range(5):
        r = client.post("/api/v1/auth/login", json={"username": "viewer1", "password": "wrong"})
        assert r.status_code == 401
    # Locked out, even with the right password (and with different case).
    for name in ("viewer1", "VIEWER1"):
        r = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) > 0
    # Other users are unaffected.
    add_user(db, "viewer2", Role.VIEWER)
    login(client, "viewer2")


def test_rate_limiter_window_and_reset() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter({"user": 2, "client": 3}, window_seconds=60, clock=lambda: now[0])
    keys = ["user:a", "client:192.0.2.1"]
    assert limiter.retry_after(keys) is None
    limiter.record_failure(keys)
    now[0] += 10
    limiter.record_failure(keys)
    assert limiter.retry_after(keys) == 51  # first failure leaves the window at t=1060
    now[0] += 51
    assert limiter.retry_after(keys) is None
    limiter.record_failure(keys)  # t=1061: the user key has 2 recent, the client key 2 of 3
    assert limiter.retry_after(["client:192.0.2.1"]) is None
    limiter.record_failure(["client:192.0.2.1"])
    assert limiter.retry_after(["client:192.0.2.1"]) is not None
    limiter.reset("user:a")
    assert limiter.retry_after(["user:a"]) is None


def test_rate_limiter_caps_tracked_keys() -> None:
    # Keys under their limit are evicted least recently used first.
    limiter = LoginRateLimiter({"user": 2}, max_keys=3)
    for i in range(10):
        limiter.record_failure([f"user:u{i}"])
    assert len(limiter._keys) == 3
    limiter.record_failure(["user:u0"])  # u0 was evicted: this is its first failure again
    assert limiter.retry_after(["user:u0"]) is None
    limiter.record_failure(["user:u9"])  # u9 was kept: this is its second failure
    assert limiter.retry_after(["user:u9"]) is not None


def test_rate_limiter_spray_cannot_evict_a_lockout() -> None:
    """Flooding the key cap with junk usernames from many addresses doesn't unlock admin."""
    limiter = LoginRateLimiter({"user": 5, "client": 50}, max_keys=100)
    for i in range(5):
        assert limiter.acquire(["user:admin", f"client:192.0.2.{i}"]) is None
    assert limiter.acquire(["user:admin", "client:192.0.2.200"]) is not None
    for i in range(5000):
        limiter.acquire([f"user:junk{i}", f"client:198.51.100.{i % 100}"])
        assert len(limiter._keys) <= 100
    assert limiter.retry_after(["user:admin"]) is not None
    assert limiter.acquire(["user:admin", "client:203.0.113.1"]) is not None


def test_rate_limiter_full_of_lockouts_fails_closed_and_recovers() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter({"user": 1}, window_seconds=60, max_keys=3, clock=lambda: now[0])
    for i in range(3):
        assert limiter.acquire([f"user:u{i}"]) is None  # each one now locked
    # No room for a new key and nothing may be evicted: refused, memory stays bounded.
    for i in range(3, 50):
        wait = limiter.acquire([f"user:u{i}"])
        assert wait is not None and 1 <= wait <= 61
    assert len(limiter._keys) == 3
    assert all(limiter.retry_after([f"user:u{i}"]) is not None for i in range(3))
    # Once the lockouts expire, logins work again.
    now[0] += 60
    assert limiter.acquire(["user:fresh"]) is None


def test_rate_limiter_near_limit_key_survives_a_spray() -> None:
    """A key one failure short of its limit is evicted after keys with fewer failures."""
    limiter = LoginRateLimiter({"user": 5, "client": 1000}, max_keys=100)
    for i in range(4):
        assert limiter.acquire(["user:admin", f"client:192.0.2.{i}"]) is None
    for i in range(5000):
        limiter.acquire([f"user:junk{i}", f"client:198.51.100.{i % 50}"])
        assert len(limiter._keys) <= 100
    # Still at 4 failures: the next guess is allowed and locks the account.
    assert limiter.acquire(["user:admin", "client:203.0.113.1"]) is None
    assert limiter.acquire(["user:admin", "client:203.0.113.1"]) is not None


class _CountingKeys(OrderedDict[str, object]):
    scans = 0

    def items(self):  # type: ignore[no-untyped-def,override]
        type(self).scans += 1
        return super().items()


def test_rate_limiter_full_of_lockouts_refuses_without_rescanning() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter({"user": 1}, window_seconds=60, max_keys=3, clock=lambda: now[0])
    limiter._keys = _CountingKeys()  # type: ignore[assignment]
    for i in range(3):
        assert limiter.acquire([f"user:u{i}"]) is None
        now[0] += 1  # u0 unlocks at 1060, u1 at 1061, u2 at 1062
    assert limiter.acquire(["user:x"]) == 58  # one scan finds no room until 1060
    scans = _CountingKeys.scans
    for i in range(100):
        assert limiter.acquire([f"user:y{i}"]) is not None
    assert _CountingKeys.scans == scans  # refused from the cache, no scan
    assert len(limiter._keys) == 3
    # Fail closed until the first lockout ends, then that slot is reused.
    now[0] = 1059.5
    assert limiter.acquire(["user:z"]) is not None
    now[0] = 1060.0
    assert limiter.acquire(["user:z"]) is None
    assert limiter.retry_after(["user:u1"]) is not None  # other lockouts untouched


def test_rate_limiter_reset_clears_the_full_cache() -> None:
    limiter = LoginRateLimiter({"user": 1}, max_keys=2)
    limiter.acquire(["user:a"])
    limiter.acquire(["user:b"])
    assert limiter.acquire(["user:c"]) is not None
    limiter.reset("user:a")
    assert limiter.acquire(["user:c"]) is None


# --- sessions, CSRF, roles -----------------------------------------------------------


def test_me_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 401
    client.cookies.set(SECURE_COOKIE, "made-up-token")
    assert client.get("/api/v1/me").status_code == 401


def test_logout_needs_csrf_and_ends_session_everywhere(client: TestClient, db: Session) -> None:
    me = do_setup(client)
    stolen = client.cookies[SECURE_COOKIE]

    assert client.post("/api/v1/auth/logout").status_code == 403
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403

    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 204
    assert SECURE_COOKIE not in client.cookies
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0

    # A copy of the cookie taken before logout no longer works.
    client.cookies.set(SECURE_COOKIE, stolen)
    assert client.get("/api/v1/me").status_code == 401


def test_csrf_token_is_bound_to_the_session(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    other = login(client, "viewer1")
    me = do_setup_or_login_admin(client, db)
    assert me["csrf_token"] != other["csrf_token"]
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": other["csrf_token"]})
    assert r.status_code == 403


def do_setup_or_login_admin(client: TestClient, db: Session) -> dict[str, str]:
    add_user(db, ADMIN, Role.ADMIN)
    return login(client, ADMIN)


def test_admin_route_refuses_viewers(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me = login(client, "viewer1")
    assert client.get("/api/v1/_test/any").json() == {"user": "viewer1"}
    r = client.post("/api/v1/_test/admin", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 403

    me = do_setup_or_login_admin(client, db)
    r = client.post("/api/v1/_test/admin", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 200


def test_admin_route_needs_csrf_too(client: TestClient) -> None:
    do_setup(client)
    assert client.post("/api/v1/_test/admin").status_code == 403


def test_demotion_applies_on_next_request(client: TestClient, db: Session) -> None:
    me = do_setup(client)
    user = db.scalars(select(User)).one()
    user.role = Role.VIEWER
    db.commit()
    r = client.post("/api/v1/_test/admin", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 403


def test_deleting_user_ends_their_sessions(client: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    login(client, "viewer1")
    db.query(User).filter(User.username == "viewer1").delete()
    db.commit()
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize(
    "field,shift",
    [("expires_at", timedelta(seconds=-1)), ("last_seen_at", timedelta(minutes=-121))],
)
def test_expired_or_idle_session_is_rejected_and_removed(
    client: TestClient, db: Session, field: str, shift: timedelta
) -> None:
    do_setup(client)
    row = db.scalars(select(UserSession)).one()
    base = utcnow()
    setattr(row, field, base + shift)
    db.commit()
    assert client.get("/api/v1/me").status_code == 401
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_activity_refreshes_last_seen(client: TestClient, db: Session) -> None:
    do_setup(client)
    row = db.scalars(select(UserSession)).one()
    stale = utcnow() - timedelta(minutes=30)
    row.last_seen_at = stale
    db.commit()
    assert client.get("/api/v1/me").status_code == 200
    db.expire_all()
    assert db.scalars(select(UserSession)).one().last_seen_at > stale


# --- request hardening ---------------------------------------------------------------


def test_cross_site_writes_are_refused(client: TestClient) -> None:
    r = client.post(
        "/api/v1/setup",
        json={"username": ADMIN, "password": PASSWORD},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert client.get("/api/v1/setup/status").json() == {"setup_required": True}


def test_api_responses_are_not_cached(client: TestClient) -> None:
    assert client.get("/api/v1/setup/status").headers["cache-control"] == "no-store"
    assert client.get("/api/v1/me").headers["cache-control"] == "no-store"


def test_passwords_verify_unknown_user_is_false() -> None:
    passwords = Passwords(FAST_HASHER)
    assert passwords.verify(None, PASSWORD) is False
    assert passwords.verify("not-a-hash", PASSWORD) is False
    assert passwords.verify(passwords.hash(PASSWORD), PASSWORD) is True


def test_rate_limiter_acquire_counts_before_the_check_finishes() -> None:
    """Parallel attempts can't all pass: each acquire counts immediately."""
    limiter = LoginRateLimiter({"user": 5, "client": 50})
    keys = ["user:admin", "client:192.0.2.1"]
    allowed = [limiter.acquire(keys) is None for _ in range(40)]
    assert allowed.count(True) == 5
    assert limiter.acquire(keys) is not None


def test_rate_limiter_success_refunds_attempt() -> None:
    limiter = LoginRateLimiter({"user": 2, "client": 2})
    for _ in range(2):
        assert limiter.acquire(["user:a", "client:192.0.2.1"]) is None
        limiter.succeeded("user:a", ["client:192.0.2.1"])
    assert limiter.retry_after(["user:a", "client:192.0.2.1"]) is None


def test_rate_limiter_eviction_keeps_active_lockout() -> None:
    limiter = LoginRateLimiter({"user": 1}, max_keys=3)
    limiter.record_failure(["user:admin"])
    for i in range(10):
        assert limiter.acquire(["user:admin"]) is not None  # attacker keeps trying admin
        limiter.record_failure([f"user:u{i}"])
    assert limiter.retry_after(["user:admin"]) is not None
