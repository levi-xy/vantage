"""User management API (M3.5): /users (admin only) and /me/password (any role)."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import LOGIN_LIMITS
from app.api.deps import CSRF_HEADER
from app.core import users
from app.core.auth import Passwords
from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox
from app.core.db import make_session_factory, utcnow
from app.main import create_app
from app.models import ActorType, AuditLog, LoginFailure, Role, User, UserSession
from tests.audit_helpers import raw_audit_table
from tests.conftest import TEST_MASTER_KEY
from tests.test_auth import ADMIN, FAST_HASHER, PASSWORD, add_user, do_setup, login

API = "/api/v1"
NEW_PASSWORD = "example-new-password-456"
OTHER_PASSWORD = "example-other-password-789"
WRONG = "example-wrong-password"


class HookedPasswords(Passwords):
    """Fast Argon2, with optional hooks that fire once, to stage races deterministically."""

    def __init__(self) -> None:
        super().__init__(FAST_HASHER)
        self.on_hash: Callable[[], None] | None = None
        self.on_verify: Callable[[], None] | None = None

    def hash(self, password: str) -> str:
        hook, self.on_hash = self.on_hash, None
        if hook is not None:
            hook()
        return super().hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        hook, self.on_verify = self.on_verify, None
        if hook is not None:
            hook()
        return super().verify(password_hash, password)


@pytest.fixture
def passwords() -> HookedPasswords:
    return HookedPasswords()


@pytest.fixture
def app(engine: Engine, db_url: str, passwords: HookedPasswords) -> Iterator[Any]:
    application = create_app(
        Settings(database_url=normalize_database_url(db_url)),
        secret_box=SecretBox(TEST_MASTER_KEY),
        passwords=passwords,
    )
    with TestClient(application, base_url="https://testserver"):  # runs the lifespan
        yield application


def browser(app: Any) -> TestClient:
    """Another browser on the same backend (its own cookie jar)."""
    return TestClient(app, base_url="https://testserver")


def logged_in(app: Any, username: str, password: str = PASSWORD) -> TestClient:
    c = browser(app)
    me = login(c, username, password)
    c.headers[CSRF_HEADER] = me["csrf_token"]
    return c


@pytest.fixture
def admin(app: Any) -> TestClient:
    c = browser(app)
    me = do_setup(c)
    c.headers[CSRF_HEADER] = me["csrf_token"]
    return c


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


def user_id(db: Session, username: str) -> int:
    db.expire_all()
    found = db.scalar(select(User.id).where(User.username == username))
    assert found is not None
    return found


def get_user(db: Session, username: str) -> User | None:
    db.expire_all()
    return db.scalar(select(User).where(User.username == username))


def audit_rows(db: Session) -> list[AuditLog]:
    db.expire_all()
    return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


def hashes(db: Session) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(User.password_hash)))


def assert_nothing_secret(engine: Engine, db: Session, *texts: str) -> None:
    """No password, and no password hash, in any response body or audit entry."""
    secrets = [PASSWORD, NEW_PASSWORD, OTHER_PASSWORD, WRONG, *hashes(db)]
    stored = raw_audit_table(engine)
    for secret in secrets:
        assert secret not in stored
        for text in texts:
            assert secret not in text
    for text in (*texts, stored):
        assert "$argon2" not in text
        assert "password_hash" not in text


# --- roles and CSRF --------------------------------------------------------------------

ADMIN_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/users", None),
    ("POST", "/users", {"username": "someone", "password": NEW_PASSWORD, "role": "viewer"}),
    ("PATCH", "/users/1", {"role": "viewer"}),
    ("POST", "/users/1/password", {"password": NEW_PASSWORD}),
    ("DELETE", "/users/1", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ROUTES)
def test_viewer_gets_403_on_user_routes(
    app: Any, admin: TestClient, db: Session, method: str, path: str, body: dict[str, Any] | None
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    viewer = logged_in(app, "viewer1")
    before = [(u.id, u.username, u.role, u.password_hash) for u in db.scalars(select(User))]
    r = viewer.request(method, API + path, json=body)
    assert r.status_code == 403
    db.expire_all()
    after = [(u.id, u.username, u.role, u.password_hash) for u in db.scalars(select(User))]
    assert after == before


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ROUTES)
def test_user_routes_need_a_session(
    app: Any, admin: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert browser(app).request(method, API + path, json=body).status_code == 401


def test_user_writes_need_csrf(admin: TestClient) -> None:
    r = admin.post(
        f"{API}/users",
        json={"username": "someone", "password": NEW_PASSWORD, "role": "viewer"},
        headers={CSRF_HEADER: ""},
    )
    assert r.status_code == 403


# --- list and create -------------------------------------------------------------------


def test_list_users_shows_no_password_or_hash(
    admin: TestClient, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    r = admin.get(f"{API}/users")
    assert r.status_code == 200
    body = r.json()
    assert [(u["username"], u["role"]) for u in body] == [(ADMIN, "admin"), ("viewer1", "viewer")]
    assert set(body[0]) == {"id", "username", "role", "created_at", "last_login_at"}
    assert body[1]["last_login_at"] is None
    assert_nothing_secret(engine, db, r.text)


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_create_user_then_they_log_in(
    app: Any, admin: TestClient, engine: Engine, db: Session, role: str
) -> None:
    r = admin.post(
        f"{API}/users", json={"username": "new1", "password": NEW_PASSWORD, "role": role}
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["username"] == "new1" and created["role"] == role
    me = login(browser(app), "new1", NEW_PASSWORD)
    assert me["role"] == role

    entry = [
        e for e in audit_rows(db) if e.action == "create" and e.target_id == str(created["id"])
    ]
    assert len(entry) == 1
    assert entry[0].actor_type == ActorType.USER
    assert entry[0].user_id == user_id(db, ADMIN)
    assert entry[0].changes == {"username": "new1", "role": role, "password": "<changed>"}
    assert_nothing_secret(engine, db, r.text, json.dumps(me))


@pytest.mark.parametrize("name", [ADMIN, "Admin", "ADMIN"])
def test_create_refuses_duplicates_ignoring_case(admin: TestClient, db: Session, name: str) -> None:
    before = len(audit_rows(db))
    r = admin.post(
        f"{API}/users", json={"username": name, "password": NEW_PASSWORD, "role": "viewer"}
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "a user with that username already exists"
    assert len(audit_rows(db)) == before
    assert NEW_PASSWORD not in r.text


@pytest.mark.parametrize(
    "body",
    [
        {"username": "new1", "password": "ex-pw-9", "role": "viewer"},
        {"username": "bad name", "password": NEW_PASSWORD, "role": "viewer"},
        {"username": "new1", "password": NEW_PASSWORD, "role": "root"},
        {"username": "new1", "password": NEW_PASSWORD, "role": "viewer", "password_hash": "x"},
    ],
)
def test_create_validates_and_never_echoes_the_password(
    admin: TestClient, db: Session, body: dict[str, str]
) -> None:
    r = admin.post(f"{API}/users", json=body)
    assert r.status_code == 422
    assert body["password"] not in r.text
    assert get_user(db, "new1") is None


def test_add_user_reports_only_a_real_duplicate(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_user(db, "taken", Role.VIEWER)
    # A concurrent create of the same name: the first check passes, the insert collides.
    real_taken = users.username_taken
    calls: list[str] = []

    def taken(session: Session, name: str) -> bool:
        calls.append(name)
        return len(calls) > 1 and real_taken(session, name)

    monkeypatch.setattr(users, "username_taken", taken)
    with pytest.raises(users.UsernameTaken):
        users.add_user(db, username="taken", password_hash="h", role=Role.VIEWER)
    assert calls == ["taken", "taken"]


def test_add_user_passes_other_integrity_errors_through(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_flush(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    monkeypatch.setattr(db, "flush", broken_flush)
    with pytest.raises(IntegrityError):
        users.add_user(db, username="fresh", password_hash="h", role=Role.VIEWER)


# --- change role -----------------------------------------------------------------------


def test_change_role_is_audited_and_takes_effect(app: Any, admin: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    viewer = logged_in(app, "viewer1")
    target = user_id(db, "viewer1")
    assert viewer.get(f"{API}/users").status_code == 403

    r = admin.patch(f"{API}/users/{target}", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # The same session now has the new role.
    assert viewer.get(f"{API}/users").status_code == 200
    last = audit_rows(db)[-1]
    assert (last.action, last.target_id, last.actor_type) == ("update", str(target), "user")
    assert last.changes == {"role": {"old": "viewer", "new": "admin"}}


def test_unchanged_role_writes_no_audit_entry(admin: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    before = len(audit_rows(db))
    r = admin.patch(f"{API}/users/{user_id(db, 'viewer1')}", json={"role": "viewer"})
    assert r.status_code == 200
    assert len(audit_rows(db)) == before


def test_last_admin_cannot_be_demoted(admin: TestClient, db: Session) -> None:
    me = user_id(db, ADMIN)
    before = len(audit_rows(db))
    r = admin.patch(f"{API}/users/{me}", json={"role": "viewer"})
    assert r.status_code == 409
    assert "last admin" in r.json()["detail"]
    user = get_user(db, ADMIN)
    assert user is not None and user.role == Role.ADMIN
    assert len(audit_rows(db)) == before


def test_admin_can_demote_themselves_while_another_admin_remains(
    admin: TestClient, db: Session
) -> None:
    add_user(db, "admin2", Role.ADMIN)
    r = admin.patch(f"{API}/users/{user_id(db, ADMIN)}", json={"role": "viewer"})
    assert r.status_code == 200
    assert admin.get(f"{API}/users").status_code == 403


def test_role_patch_accepts_only_the_role(admin: TestClient, db: Session) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")
    for body in ({"username": "renamed"}, {"role": "viewer", "password": NEW_PASSWORD}, {}):
        r = admin.patch(f"{API}/users/{target}", json=body)
        assert r.status_code == 422
        assert NEW_PASSWORD not in r.text


def test_unknown_user_is_404(admin: TestClient) -> None:
    assert admin.patch(f"{API}/users/999", json={"role": "viewer"}).status_code == 404
    assert (
        admin.post(f"{API}/users/999/password", json={"password": NEW_PASSWORD}).status_code == 404
    )
    assert admin.delete(f"{API}/users/999").status_code == 404


def _demote_meanwhile(engine: Engine, username: str) -> Callable[[Session], None]:
    """A lock_admins that first lets another admin's demotion of ``username`` commit:
    the other half of two admins demoting each other at the same time."""
    real = users.lock_admins

    def lock(db: Session) -> None:
        with make_session_factory(engine)() as other:
            other.execute(
                User.__table__.update().where(User.username == username).values(role="viewer")
            )
            other.commit()
        real(db)

    return lock


def test_two_admins_cannot_demote_each_other(
    admin: TestClient, engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_user(db, "admin2", Role.ADMIN)
    monkeypatch.setattr(users, "lock_admins", _demote_meanwhile(engine, ADMIN))
    # The actor was authenticated as an admin, but is demoted before the change lands.
    r = admin.patch(f"{API}/users/{user_id(db, 'admin2')}", json={"role": "viewer"})
    assert r.status_code == 409
    other = get_user(db, "admin2")
    assert other is not None and other.role == Role.ADMIN


def test_delete_cannot_remove_the_last_admin(
    admin: TestClient, engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_user(db, "admin2", Role.ADMIN)
    monkeypatch.setattr(users, "lock_admins", _demote_meanwhile(engine, ADMIN))
    r = admin.delete(f"{API}/users/{user_id(db, 'admin2')}")
    assert r.status_code == 409
    assert get_user(db, "admin2") is not None


# --- reset another user's password -----------------------------------------------------


def test_reset_ends_that_users_sessions_only(
    app: Any, admin: TestClient, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")
    phone, laptop = logged_in(app, "viewer1"), logged_in(app, "viewer1")
    db.add(LoginFailure(user_id=target, at=utcnow()))
    db.commit()

    r = admin.post(f"{API}/users/{target}/password", json={"password": NEW_PASSWORD})
    assert r.status_code == 204 and r.content == b""
    assert phone.get(f"{API}/me").status_code == 401
    assert laptop.get(f"{API}/me").status_code == 401
    assert admin.get(f"{API}/me").status_code == 200  # the admin's own session stays

    assert (
        browser(app)
        .post(f"{API}/auth/login", json={"username": "viewer1", "password": PASSWORD})
        .status_code
        == 401
    )
    login(browser(app), "viewer1", NEW_PASSWORD)

    entry = next(e for e in audit_rows(db) if e.action == "update" and e.target_id == str(target))
    assert entry.actor_type == ActorType.USER and entry.user_id == user_id(db, ADMIN)
    assert entry.changes == {
        "password": "<changed>",
        "sessions_ended": 2,
        "failed_logins_cleared": 1,
    }
    assert_nothing_secret(engine, db, r.text)


def test_reset_of_your_own_password_here_ends_your_session(admin: TestClient, db: Session) -> None:
    r = admin.post(f"{API}/users/{user_id(db, ADMIN)}/password", json={"password": NEW_PASSWORD})
    assert r.status_code == 204
    assert admin.get(f"{API}/me").status_code == 401


def test_reset_validates_the_password(admin: TestClient, db: Session) -> None:
    for body in ({"password": "ex-pw-9"}, {"password": NEW_PASSWORD, "role": "admin"}, {}):
        r = admin.post(f"{API}/users/{user_id(db, ADMIN)}/password", json=body)
        assert r.status_code == 422
        assert "ex-pw-9" not in r.text and NEW_PASSWORD not in r.text


def test_reset_writes_the_user_row_before_ending_sessions(
    admin: TestClient, engine: Engine, db: Session
) -> None:
    """The order that makes a racing login lose (see app.core.users)."""
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")
    statements: list[str] = []

    def seen(_conn: object, _cursor: object, sql: str, *_args: object) -> None:
        head = " ".join(sql.split()[:3]).upper()
        if head.startswith(("UPDATE USERS", "DELETE FROM SESSIONS")):
            statements.append(head)

    event.listen(Engine, "before_cursor_execute", seen)
    try:
        r = admin.post(f"{API}/users/{target}/password", json={"password": NEW_PASSWORD})
    finally:
        event.remove(Engine, "before_cursor_execute", seen)
    assert r.status_code == 204
    assert statements == ["UPDATE USERS SET", "DELETE FROM SESSIONS"]


def test_login_holding_the_row_first_loses_its_session_to_the_reset(
    app: Any, admin: TestClient, db: Session, passwords: HookedPasswords
) -> None:
    """The login takes the row lock and commits its session before the reset writes the
    row (here: during the reset's hash). The reset then ends that session."""
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")
    racer = browser(app)
    result: dict[str, int] = {}

    def login_now() -> None:
        r = racer.post(f"{API}/auth/login", json={"username": "viewer1", "password": PASSWORD})
        result["status"] = r.status_code

    passwords.on_hash = login_now
    r = admin.post(f"{API}/users/{target}/password", json={"password": NEW_PASSWORD})
    assert r.status_code == 204
    assert result == {"status": 200}  # the login won the row...
    assert racer.get(f"{API}/me").status_code == 401  # ...and lost its session
    db.expire_all()
    assert db.scalars(select(UserSession).where(UserSession.user_id == target)).all() == []
    user = get_user(db, "viewer1")
    assert user is not None and FAST_HASHER.verify(user.password_hash, NEW_PASSWORD)


def test_reset_racing_a_login_that_already_verified(
    app: Any, admin: TestClient, db: Session, passwords: HookedPasswords
) -> None:
    """The other order: the reset commits while the login checks the old password."""
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")

    def reset_now() -> None:
        r = admin.post(f"{API}/users/{target}/password", json={"password": NEW_PASSWORD})
        assert r.status_code == 204

    passwords.on_verify = reset_now
    racer = browser(app)
    r = racer.post(f"{API}/auth/login", json={"username": "viewer1", "password": PASSWORD})
    assert r.status_code == 401
    assert racer.get(f"{API}/me").status_code == 401
    db.expire_all()
    assert db.scalars(select(UserSession).where(UserSession.user_id == target)).all() == []


# --- delete ----------------------------------------------------------------------------


def test_delete_user_ends_sessions_and_keeps_history(
    app: Any, admin: TestClient, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")
    viewer = logged_in(app, "viewer1")
    r = admin.delete(f"{API}/users/{target}")
    assert r.status_code == 204
    assert viewer.get(f"{API}/me").status_code == 401
    assert get_user(db, "viewer1") is None
    rows = audit_rows(db)
    # The viewer's login entry survives, without the deleted user's id.
    assert any(
        e.action == "login" and e.target_id == str(target) and e.user_id is None for e in rows
    )
    last = rows[-1]
    assert (last.action, last.target_id, last.actor_type) == ("delete", str(target), "user")
    assert last.user_id == user_id(db, ADMIN)
    assert last.changes == {"username": "viewer1", "role": "viewer"}


def test_delete_another_admin(admin: TestClient, db: Session) -> None:
    add_user(db, "admin2", Role.ADMIN)
    assert admin.delete(f"{API}/users/{user_id(db, 'admin2')}").status_code == 204


def test_admin_cannot_delete_themselves(admin: TestClient, db: Session) -> None:
    add_user(db, "admin2", Role.ADMIN)
    r = admin.delete(f"{API}/users/{user_id(db, ADMIN)}")
    assert r.status_code == 409
    assert get_user(db, ADMIN) is not None


# --- login racing a delete (PR #27, PR #34) --------------------------------------------


@pytest.mark.parametrize("password", [PASSWORD, WRONG], ids=["right", "wrong"])
def test_user_deleted_during_their_login_gets_401(
    app: Any, admin: TestClient, db: Session, passwords: HookedPasswords, password: str
) -> None:
    """Deleted after the login_failures commit, before the password check finishes.

    With the right password the login loses the row update (the user is gone) and takes
    the rollback path, which used to reload the expired user and answer 500.
    """
    add_user(db, "viewer1", Role.VIEWER)
    target = user_id(db, "viewer1")

    def delete_now() -> None:
        assert admin.delete(f"{API}/users/{target}").status_code == 204

    passwords.on_verify = delete_now
    racer = browser(app)
    r = racer.post(f"{API}/auth/login", json={"username": "viewer1", "password": password})
    assert r.status_code == 401
    assert racer.get(f"{API}/me").status_code == 401
    db.expire_all()
    assert db.scalars(select(UserSession).where(UserSession.user_id == target)).all() == []
    assert db.scalars(select(LoginFailure).where(LoginFailure.user_id == target)).all() == []


# --- change your own password ----------------------------------------------------------


def test_change_my_password_keeps_this_session_and_ends_the_others(
    app: Any, admin: TestClient, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me, other = logged_in(app, "viewer1"), logged_in(app, "viewer1")
    r = me.post(
        f"{API}/me/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 204, r.text
    assert me.get(f"{API}/me").status_code == 200
    assert other.get(f"{API}/me").status_code == 401
    assert admin.get(f"{API}/me").status_code == 200  # someone else's session
    login(browser(app), "viewer1", NEW_PASSWORD)

    entry = audit_rows(db)[-2]  # the last one is the login just above
    target = user_id(db, "viewer1")
    assert (entry.action, entry.target_id, entry.user_id) == ("update", str(target), target)
    assert entry.actor_type == ActorType.USER
    assert entry.changes == {"password": "<changed>", "sessions_ended": 1}
    assert_nothing_secret(engine, db, r.text)


def test_change_my_password_needs_the_current_one(
    app: Any, admin: TestClient, engine: Engine, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me = logged_in(app, "viewer1")
    r = me.post(
        f"{API}/me/password", json={"current_password": WRONG, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 400
    assert me.get(f"{API}/me").status_code == 200
    login(browser(app), "viewer1", PASSWORD)  # unchanged
    assert_nothing_secret(engine, db, r.text)


def test_wrong_current_passwords_lock_like_failed_logins(
    app: Any, admin: TestClient, db: Session
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me = logged_in(app, "viewer1")
    body = {"current_password": WRONG, "new_password": NEW_PASSWORD}
    codes = [
        me.post(f"{API}/me/password", json=body).status_code
        for _ in range(LOGIN_LIMITS["user"] + 1)
    ]
    assert codes == [400] * LOGIN_LIMITS["user"] + [429]
    # The right one is refused too while locked, and so is the login form.
    right = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert me.post(f"{API}/me/password", json=right).status_code == 429
    assert (
        browser(app)
        .post(f"{API}/auth/login", json={"username": "viewer1", "password": PASSWORD})
        .status_code
        == 429
    )


def test_change_my_password_validates(app: Any, admin: TestClient) -> None:
    for body in (
        {"current_password": PASSWORD, "new_password": "ex-pw-9"},
        {"current_password": PASSWORD},
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "username": "x"},
    ):
        r = admin.post(f"{API}/me/password", json=body)
        assert r.status_code == 422
        assert PASSWORD not in r.text and NEW_PASSWORD not in r.text


def test_change_my_password_needs_csrf_and_a_session(app: Any, admin: TestClient) -> None:
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert browser(app).post(f"{API}/me/password", json=body).status_code == 401
    assert admin.post(f"{API}/me/password", json=body, headers={CSRF_HEADER: ""}).status_code == 403


def test_change_my_password_loses_to_a_reset_meanwhile(
    app: Any, admin: TestClient, db: Session, passwords: HookedPasswords
) -> None:
    add_user(db, "viewer1", Role.VIEWER)
    me = logged_in(app, "viewer1")
    target = user_id(db, "viewer1")

    def reset_now() -> None:
        r = admin.post(f"{API}/users/{target}/password", json={"password": OTHER_PASSWORD})
        assert r.status_code == 204

    passwords.on_verify = reset_now
    r = me.post(
        f"{API}/me/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 409
    user = get_user(db, "viewer1")
    assert user is not None and FAST_HASHER.verify(user.password_hash, OTHER_PASSWORD)
