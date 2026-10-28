"""Persistent per-user login failure counters (M1.8)."""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, inspect, select
from sqlalchemy.orm import Session

from app.api.auth import LOGIN_LIMITS, LOGIN_WINDOW_SECONDS
from app.core.auth import LoginRateLimiter
from app.core.db import make_session_factory, utcnow
from app.core.login_failures import UserLoginFailures
from app.models import LoginFailure, Role, User
from tests.test_auth import ADMIN, PASSWORD, add_user, login, make_client
from tests.test_client_address import PROXY, TRUSTED

USER_LIMIT = LOGIN_LIMITS["user"]
WINDOW = timedelta(seconds=LOGIN_WINDOW_SECONDS)
WRONG = "example-wrong-password"


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def rows(db: Session, user_id: int) -> int:
    db.expire_all()
    count = db.scalar(
        select(func.count()).select_from(LoginFailure).where(LoginFailure.user_id == user_id)
    )
    return count or 0


def attempt(client: TestClient, username: str, password: str, real_ip: str | None = None) -> int:
    headers = {"X-Real-IP": real_ip} if real_ip else {}
    r = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}, headers=headers
    )
    return r.status_code


# --- the counter -----------------------------------------------------------------------


def test_counter_locks_after_limit_and_expires(db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    clock = Clock()
    counter = UserLoginFailures(3, window=WINDOW, clock=clock)

    for _ in range(3):
        assert counter.acquire(db, user.id) is None
        clock.advance(timedelta(minutes=1))
    # The 4th attempt (3 minutes in) is refused until the first failure leaves the
    # window, 12 minutes from now; a refused attempt counts nothing.
    wait = counter.acquire(db, user.id)
    assert wait is not None
    assert 12 * 60 < wait <= 12 * 60 + 1
    assert rows(db, user.id) == 3

    # Just before the first failure expires: still locked.
    clock.advance(timedelta(seconds=wait - 2))
    assert counter.acquire(db, user.id) is not None
    # Sliding window: once the oldest leaves, one more attempt is allowed, not three.
    clock.advance(timedelta(seconds=2))
    assert counter.acquire(db, user.id) is None
    assert counter.acquire(db, user.id) is not None

    # Long after: everything expired, and the expired rows were pruned.
    clock.advance(WINDOW * 2)
    assert counter.acquire(db, user.id) is None
    assert rows(db, user.id) == 1


def test_counter_is_per_user(db: Session) -> None:
    a = add_user(db, "viewer1", Role.VIEWER)
    b = add_user(db, "viewer2", Role.VIEWER)
    counter = UserLoginFailures(2, window=WINDOW)
    assert counter.acquire(db, a.id) is None
    assert counter.acquire(db, a.id) is None
    assert counter.acquire(db, a.id) is not None
    assert counter.acquire(db, b.id) is None


def test_clear_removes_every_failure(db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    counter = UserLoginFailures(2, window=WINDOW)
    counter.acquire(db, user.id)
    counter.acquire(db, user.id)
    assert UserLoginFailures.clear(db, user.id) == 2
    db.commit()
    assert rows(db, user.id) == 0
    assert counter.acquire(db, user.id) is None


def test_counter_for_deleted_user_counts_nothing(db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    user_id = user.id
    db.execute(delete(User).where(User.id == user_id))
    db.commit()
    assert UserLoginFailures(2, window=WINDOW).acquire(db, user_id) is None
    assert rows(db, user_id) == 0


def test_deleting_a_user_deletes_their_failures(db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    user_id = user.id
    UserLoginFailures(2, window=WINDOW).acquire(db, user_id)
    db.execute(delete(User).where(User.id == user_id))
    db.commit()
    assert rows(db, user_id) == 0


def test_counter_rejects_impossible_limit() -> None:
    with pytest.raises(ValueError):
        UserLoginFailures(0)


def test_table_holds_only_who_and_when(engine: Engine) -> None:
    columns = {c["name"] for c in inspect(engine).get_columns("login_failures")}
    assert columns == {"id", "user_id", "at"}


# --- through the login API ---------------------------------------------------------------


def test_spray_of_junk_usernames_cannot_unlock_a_user(
    db_url: str, engine: Engine, db: Session
) -> None:
    """The PR #23 review scenario: 4 guesses on admin, then a spray of junk usernames
    from many addresses meant to evict admin's counter, then the 5th guess locks admin."""
    add_user(db, ADMIN, Role.ADMIN)
    with make_client(db_url, peer=PROXY, trusted_proxies=TRUSTED) as proxy:
        # A small in-memory table, so the spray fills it many times over: with the old
        # in-memory per-user key, this evicted admin's count.
        proxy.app.state.login_limiter = LoginRateLimiter(  # type: ignore[attr-defined]
            LOGIN_LIMITS, window_seconds=LOGIN_WINDOW_SECONDS, max_keys=64
        )
        for _ in range(USER_LIMIT - 1):
            assert attempt(proxy, ADMIN, WRONG, real_ip="192.0.2.10") == 401

        # 500 junk usernames x 4 attempts each (as near the limit as admin), spread over
        # 200 addresses so no client limit is reached.
        statuses = set()
        for i in range(2000):
            junk, address = f"junk{i // 4}", f"198.51.100.{i % 200}"
            statuses.add(attempt(proxy, junk, WRONG, real_ip=address))
        assert statuses <= {401, 429}

        # Admin's count survived: the 5th guess is allowed, the 6th is not, from anywhere
        # and even with the right password.
        assert attempt(proxy, ADMIN, WRONG, real_ip="203.0.113.5") == 401
        assert attempt(proxy, ADMIN, PASSWORD, real_ip="203.0.113.6") == 429
        assert attempt(proxy, "ADMIN", PASSWORD, real_ip="203.0.113.7") == 429

    admin_id = db.scalars(select(User.id).where(User.username == ADMIN)).one()
    assert rows(db, admin_id) == USER_LIMIT
    # Junk usernames match no account, so they never wrote a row.
    assert db.scalar(select(func.count()).select_from(LoginFailure)) == USER_LIMIT


def test_lockout_survives_a_restart(db_url: str, engine: Engine, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url) as before:
        for _ in range(USER_LIMIT):
            assert attempt(before, "viewer1", WRONG) == 401
    # A new app (fresh in-memory limiter) on the same database.
    with make_client(db_url) as after:
        assert attempt(after, "viewer1", PASSWORD) == 429


def test_refused_attempts_add_no_rows(db_url: str, engine: Engine, db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url, peer=PROXY, trusted_proxies=TRUSTED) as proxy:
        for i in range(USER_LIMIT + 20):
            attempt(proxy, "viewer1", WRONG, real_ip=f"198.51.100.{i}")
    assert rows(db, user.id) == USER_LIMIT


def test_successful_login_clears_failures(db_url: str, engine: Engine, db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url) as client:
        for _ in range(USER_LIMIT - 1):
            assert attempt(client, "viewer1", WRONG) == 401
        login(client, "viewer1")
        assert rows(db, user.id) == 0
        # A full budget again.
        for _ in range(USER_LIMIT):
            assert attempt(client, "viewer1", WRONG) == 401
        assert attempt(client, "viewer1", PASSWORD) == 429


def test_case_variant_counts_on_the_account(db_url: str, engine: Engine, db: Session) -> None:
    user = add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url) as client:
        # Login compares names exactly, so a case variant fails even with the right
        # password, but it counts on the account it resembles.
        for name in ("VIEWER1", "Viewer1", "vIEWER1", "VIEWer1"):
            assert attempt(client, name, PASSWORD) == 401
        assert rows(db, user.id) == USER_LIMIT - 1
        assert attempt(client, "VIEWER1", PASSWORD) == 401
        assert attempt(client, "viewer1", PASSWORD) == 429
    assert db.scalar(select(func.count()).select_from(LoginFailure)) == USER_LIMIT


def test_unknown_usernames_are_still_limited_in_memory(
    db_url: str, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url, peer=PROXY, trusted_proxies=TRUSTED) as proxy:
        for i in range(USER_LIMIT):
            assert attempt(proxy, "nobody", WRONG, real_ip=f"198.51.100.{i}") == 401
        assert attempt(proxy, "nobody", WRONG, real_ip="198.51.100.200") == 429
    assert db.scalar(select(func.count()).select_from(LoginFailure)) == 0


def test_nothing_secret_is_stored(db_url: str, engine: Engine, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    with make_client(db_url, peer=PROXY, trusted_proxies=TRUSTED) as proxy:
        for _ in range(3):
            attempt(proxy, "viewer1", WRONG, real_ip="192.0.2.77")
    db.expire_all()
    stored = json.dumps(
        [[r.id, r.user_id, r.at.isoformat()] for r in db.scalars(select(LoginFailure))]
    )
    assert len(json.loads(stored)) == 3
    for value in (WRONG, PASSWORD, "viewer1", "192.0.2.77", PROXY):
        assert value not in stored
