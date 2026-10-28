"""Admin CLI (M1.6): reset-password, create-user, list-users."""

import io
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.auth import SETUP_MARKER
from app.cli import AUDIT_SOURCE, LIMITER_HINT, Console, build_parser, main
from app.collectors.scheduler import PollScheduler
from app.core.auth import Passwords
from app.core.config import Settings, normalize_database_url
from app.core.crypto import SecretBox
from app.core.db import make_session_factory
from app.main import create_app
from app.models import AuditLog, Role, Setting, User, UserSession
from tests.conftest import BACKEND_DIR, TEST_MASTER_KEY
from tests.test_auth import ADMIN, FAST_HASHER, PASSWORD, add_user, do_setup, login, make_client

NEW_PASSWORD = "example-new-password-456"


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


class Prompt:
    """Stands in for getpass: hands out answers in order and records the prompts."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def cli(engine: Engine, db_url: str) -> "CliRunner":
    return CliRunner(db_url)


class CliRunner:
    def __init__(self, db_url: str) -> None:
        self.settings = Settings(database_url=normalize_database_url(db_url))
        self.passwords = Passwords(FAST_HASHER)

    def __call__(self, *argv: str, stdin: str | None = None, prompt: Prompt | None = None) -> Run:
        out, err = io.StringIO(), io.StringIO()
        console = Console(
            stdin=io.StringIO(stdin) if stdin is not None else FakeTty(),
            stdout=out,
            stderr=err,
            getpass=prompt or Prompt(),
        )
        code = main(list(argv), console=console, settings=self.settings, passwords=self.passwords)
        return Run(code, out.getvalue(), err.getvalue())


def audit_rows(db: Session) -> list[AuditLog]:
    db.expire_all()
    return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


def raw_audit_text(db: Session) -> str:
    return json.dumps(
        [[r.user_id, r.action, r.target_type, r.target_id, r.changes] for r in audit_rows(db)]
    )


def get_user(db: Session, username: str) -> User:
    db.expire_all()
    return db.scalars(select(User).where(User.username == username)).one()


# --- reset-password --------------------------------------------------------------------


def test_reset_ends_sessions_and_swaps_password(
    cli: CliRunner, db: Session, db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with make_client(db_url) as browser, make_client(db_url) as other:
        do_setup(browser)
        login(other, ADMIN)
        assert browser.get("/api/v1/me").status_code == 200
        assert other.get("/api/v1/me").status_code == 200
        old_hash = get_user(db, ADMIN).password_hash

        run = cli("reset-password", ADMIN, prompt=Prompt(NEW_PASSWORD, NEW_PASSWORD))
        assert run.code == 0, run.err
        assert "ended 2 session(s)" in run.out
        assert LIMITER_HINT in run.out

        # Sessions from before the reset are dead, through the real API.
        assert browser.get("/api/v1/me").status_code == 401
        assert other.get("/api/v1/me").status_code == 401
        db.expire_all()
        assert db.scalars(select(UserSession)).all() == []

        # Old password refused, new one works.
        r = browser.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
        assert r.status_code == 401
        login(browser, ADMIN, NEW_PASSWORD)
        assert browser.get("/api/v1/me").status_code == 200

    new_hash = get_user(db, ADMIN).password_hash
    assert new_hash != old_hash and new_hash.startswith("$argon2id$")

    # Audited as an update on the user, by the CLI, with the password redacted.
    entry = next(r for r in audit_rows(db) if r.action == "update")
    assert entry.target_type == "user"
    assert entry.target_id == str(get_user(db, ADMIN).id)
    assert entry.user_id is None
    assert entry.changes == {"password": "<changed>", "sessions_ended": 2, "source": AUDIT_SOURCE}

    # No password or hash anywhere: audit table, CLI output, logs.
    everything = raw_audit_text(db) + run.out + run.err + caplog.text
    for secret in (NEW_PASSWORD, PASSWORD, new_hash, old_hash, "$argon2"):
        assert secret not in everything


def test_reset_only_touches_that_users_sessions(cli: CliRunner, db: Session, db_url: str) -> None:
    with make_client(db_url) as admin_browser, make_client(db_url) as viewer_browser:
        do_setup(admin_browser)
        add_user(db, "viewer1", Role.VIEWER)
        login(viewer_browser, "viewer1")

        run = cli("reset-password", "viewer1", "--password-stdin", stdin=NEW_PASSWORD + "\n")
        assert run.code == 0, run.err
        assert viewer_browser.get("/api/v1/me").status_code == 401
        assert admin_browser.get("/api/v1/me").status_code == 200


def test_reset_with_password_stdin(cli: CliRunner, db: Session, db_url: str) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    # Windows line ending: only the line break is stripped.
    run = cli("reset-password", ADMIN, "--password-stdin", stdin=f" {NEW_PASSWORD} \r\nignored\n")
    assert run.code == 0, run.err
    assert NEW_PASSWORD not in run.out + run.err
    with make_client(db_url) as browser:
        login(browser, ADMIN, f" {NEW_PASSWORD} ")


def test_reset_unknown_user(cli: CliRunner, db: Session) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    prompt = Prompt(NEW_PASSWORD, NEW_PASSWORD)
    run = cli("reset-password", "Admin", prompt=prompt)
    assert run.code == 1
    assert "no user with that username" in run.err
    assert "Admin" not in run.err  # the typed text isn't repeated
    assert prompt.prompts == []  # not asked for a password at all
    assert audit_rows(db) == []


def test_reset_prompt_mismatch_changes_nothing(cli: CliRunner, db: Session) -> None:
    user = add_user(db, ADMIN, Role.ADMIN)
    old_hash = user.password_hash
    run = cli("reset-password", ADMIN, prompt=Prompt(NEW_PASSWORD, NEW_PASSWORD + "x"))
    assert run.code == 1
    assert "don't match" in run.err
    assert NEW_PASSWORD not in run.err + run.out
    assert get_user(db, ADMIN).password_hash == old_hash
    assert audit_rows(db) == []


@pytest.mark.parametrize("bad", ["", "short-pw-11", "x" * 1025])
def test_reset_refuses_passwords_setup_refuses(cli: CliRunner, db: Session, bad: str) -> None:
    user = add_user(db, ADMIN, Role.ADMIN)
    old_hash = user.password_hash
    run = cli("reset-password", ADMIN, "--password-stdin", stdin=bad + "\n")
    assert run.code == 1
    assert "12 to 1024 characters" in run.err
    if bad:
        assert bad not in run.err + run.out
    assert get_user(db, ADMIN).password_hash == old_hash
    assert audit_rows(db) == []


def test_prompt_refused_without_a_terminal(cli: CliRunner, db: Session) -> None:
    # getpass would fall back to reading stdin with echo on.
    add_user(db, ADMIN, Role.ADMIN)
    prompt = Prompt(NEW_PASSWORD, NEW_PASSWORD)
    run = cli("reset-password", ADMIN, stdin=NEW_PASSWORD + "\n", prompt=prompt)
    assert run.code == 1
    assert "--password-stdin" in run.err
    assert prompt.prompts == []


def test_cancelled_prompt(cli: CliRunner, db: Session) -> None:
    add_user(db, ADMIN, Role.ADMIN)

    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    out, err = io.StringIO(), io.StringIO()
    console = Console(stdin=FakeTty(), stdout=out, stderr=err, getpass=interrupted)
    code = main(
        ["reset-password", ADMIN], console=console, settings=cli.settings, passwords=cli.passwords
    )
    assert code == 1
    assert "cancelled" in err.getvalue()


# --- create-user -----------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_create_user_then_login(cli: CliRunner, db: Session, db_url: str, role: str) -> None:
    prompt = Prompt(NEW_PASSWORD, NEW_PASSWORD)
    run = cli("create-user", "ops.user", "--role", role, prompt=prompt)
    assert run.code == 0, run.err
    assert len(prompt.prompts) == 2

    with make_client(db_url) as browser:
        me = login(browser, "ops.user", NEW_PASSWORD)
        assert me["role"] == role
        # The first user created by the CLI closes network setup, like setup itself.
        assert browser.get("/api/v1/setup/status").json() == {"setup_required": False}
    db.expire_all()
    assert db.get(Setting, SETUP_MARKER) is not None

    user = get_user(db, "ops.user")
    entry = audit_rows(db)[0]
    assert (entry.action, entry.target_type, entry.target_id) == ("create", "user", str(user.id))
    assert entry.user_id is None
    assert entry.changes == {
        "username": "ops.user",
        "role": role,
        "password": "<changed>",
        "source": AUDIT_SOURCE,
    }
    everything = raw_audit_text(db) + run.out + run.err
    for secret in (NEW_PASSWORD, user.password_hash):
        assert secret not in everything


def test_create_user_with_password_stdin(cli: CliRunner, db_url: str) -> None:
    run = cli("create-user", "viewer1", "--role", "viewer", "--password-stdin", stdin=NEW_PASSWORD)
    assert run.code == 0, run.err
    with make_client(db_url) as browser:
        assert login(browser, "viewer1", NEW_PASSWORD)["role"] == "viewer"


@pytest.mark.parametrize("name", [ADMIN, "ADMIN", "Admin"])
def test_create_user_refuses_duplicates(cli: CliRunner, db: Session, name: str) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    prompt = Prompt(NEW_PASSWORD, NEW_PASSWORD)
    run = cli("create-user", name, "--role", "viewer", prompt=prompt)
    assert run.code == 1
    assert "already exists" in run.err
    assert prompt.prompts == []
    db.expire_all()
    assert len(db.scalars(select(User)).all()) == 1
    assert audit_rows(db) == []


@pytest.mark.parametrize(
    "name", ["", "_leading", ".leading", "has space", "a" * 65, "admin\n", "é"]
)
def test_create_user_refuses_invalid_usernames(cli: CliRunner, db: Session, name: str) -> None:
    prompt = Prompt(NEW_PASSWORD, NEW_PASSWORD)
    run = cli("create-user", name, "--role", "admin", prompt=prompt)
    assert run.code == 1
    assert "invalid username" in run.err
    assert prompt.prompts == []
    assert db.scalars(select(User)).all() == []


def test_create_user_refuses_short_password(cli: CliRunner, db: Session) -> None:
    run = cli("create-user", "viewer1", "--role", "viewer", prompt=Prompt("short", "short"))
    assert run.code == 1
    assert "12 to 1024 characters" in run.err
    assert db.scalars(select(User)).all() == []


@pytest.mark.parametrize("argv", [["create-user", "x"], ["create-user", "x", "--role", "root"]])
def test_create_user_needs_a_valid_role(
    cli: CliRunner, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert cli(*argv).code == 2
    assert "root" not in capsys.readouterr().err


# --- list-users ------------------------------------------------------------------------


def test_list_users_shows_no_hashes(cli: CliRunner, db: Session) -> None:
    admin = add_user(db, ADMIN, Role.ADMIN)
    add_user(db, "viewer1", Role.VIEWER)
    run = cli("list-users")
    assert run.code == 0, run.err
    lines = run.out.splitlines()
    assert lines[0].split() == ["ID", "USERNAME", "ROLE", "CREATED_AT", "LAST_LOGIN_AT"]
    assert lines[1].split()[:3] == [str(admin.id), ADMIN, "admin"]
    assert lines[1].split()[4] == "never"
    assert lines[2].split()[1:3] == ["viewer1", "viewer"]
    assert "$argon2" not in run.out
    assert admin.password_hash not in run.out


def test_list_users_empty(cli: CliRunner) -> None:
    run = cli("list-users")
    assert run.code == 0
    assert run.out.split() == ["ID", "USERNAME", "ROLE", "CREATED_AT", "LAST_LOGIN_AT"]


# --- passwords never on the command line ----------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["reset-password", ADMIN, "--password", "example-hunter2-pw"],
        ["reset-password", ADMIN, "--password=example-hunter2-pw"],
        ["reset-password", ADMIN, "example-hunter2-pw"],
        ["reset-password", ADMIN, "--password-stdin=example-hunter2-pw"],
        ["create-user", ADMIN, "--role", "admin", "--password", "example-hunter2-pw"],
        ["create-user", ADMIN, "--role", "example-hunter2-pw"],
        ["example-hunter2-pw"],
    ],
)
def test_parser_rejects_passwords_on_argv(
    cli: CliRunner, db: Session, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert cli(*argv).code == 2
    captured = capsys.readouterr()
    assert "example-hunter2-pw" not in captured.out + captured.err
    assert audit_rows(db) == []


def test_parser_has_no_password_option() -> None:
    parser = build_parser()
    subparsers = next(a for a in parser._actions if a.dest == "command")
    for sub in subparsers.choices.values():  # type: ignore[union-attr]
        flags = {flag for action in sub._actions for flag in action.option_strings}
        assert not any(f.startswith("--password") and f != "--password-stdin" for f in flags)


# --- database checks -------------------------------------------------------------------


def test_missing_sqlite_file_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    err = io.StringIO()
    code = main(
        ["list-users"],
        console=Console(stdout=io.StringIO(), stderr=err),
        settings=Settings(database_url=f"sqlite:///{path}"),
    )
    assert code == 1
    assert "database file not found" in err.getvalue()
    assert str(path) not in err.getvalue()
    assert not path.exists()


def test_unmigrated_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    err = io.StringIO()
    code = main(
        ["list-users"],
        console=Console(stdout=io.StringIO(), stderr=err),
        settings=Settings(database_url=f"sqlite:///{path}"),
    )
    assert code == 1
    assert "no Vantage tables" in err.getvalue()


def test_cli_never_starts_the_scheduler(
    cli: CliRunner, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the CLI must not start the poll scheduler")

    monkeypatch.setattr(PollScheduler, "__init__", refuse)
    add_user(db, ADMIN, Role.ADMIN)
    assert cli("list-users").code == 0
    assert cli("reset-password", ADMIN, "--password-stdin", stdin=NEW_PASSWORD).code == 0
    assert (
        cli(
            "create-user", "viewer1", "--role", "viewer", "--password-stdin", stdin=NEW_PASSWORD
        ).code
        == 0
    )


# --- the real entry point --------------------------------------------------------------

_PROBE = """
import runpy, sys
sys.argv = ["app.cli", *sys.argv[1:]]
try:
    runpy.run_module("app.cli", run_name="__main__")
except SystemExit as exc:
    code = exc.code
loaded = sorted(m for m in ("app.main", "uvicorn") if m in sys.modules)
print("LOADED=" + ",".join(loaded), file=sys.stderr)
sys.exit(code)
"""


def test_python_m_app_cli_without_master_key(engine: Engine, db_url: str) -> None:
    """``python -m app.cli`` works with only the database configured: no master key, and
    it never loads the web app or the scheduler."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VANTAGE_")}
    env["VANTAGE_DATABASE_URL"] = db_url

    def run(*argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 (fixed command, test input)
            [sys.executable, "-c", _PROBE, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=BACKEND_DIR,
            env=env,
            timeout=60,
            check=False,
        )

    created = run("create-user", ADMIN, "--role", "admin", "--password-stdin", stdin=PASSWORD)
    assert created.returncode == 0, created.stderr
    assert "LOADED=\n" in created.stderr

    listed = run("list-users")
    assert listed.returncode == 0, listed.stderr
    assert ADMIN in listed.stdout and "$argon2" not in listed.stdout

    # Real (production) Argon2 parameters: the API's own verifier accepts the hash.
    with make_client(db_url) as browser:
        login(browser, ADMIN)

    reset = run("reset-password", ADMIN, "--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert reset.returncode == 0, reset.stderr
    unknown = run("reset-password", "nobody", "--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert unknown.returncode == 1
    for proc in (created, listed, reset, unknown):
        assert PASSWORD not in proc.stdout + proc.stderr
        assert NEW_PASSWORD not in proc.stdout + proc.stderr


# --- login racing a reset, stdin edge cases, no-admin warning -------------------------


class ResetDuringVerify(Passwords):
    """Runs a CLI reset right after the API verified the old password, before the login
    writes anything: the window a real concurrent reset can land in."""

    def __init__(self, cli: CliRunner, rehash: bool) -> None:
        super().__init__(FAST_HASHER)
        self.cli, self.rehash, self.fired = cli, rehash, False

    def verify(self, password_hash: str | None, password: str) -> bool:
        ok = super().verify(password_hash, password)
        if ok and not self.fired:
            self.fired = True
            run = self.cli("reset-password", ADMIN, "--password-stdin", stdin=NEW_PASSWORD)
            assert run.code == 0, run.err
        return ok

    def needs_rehash(self, password_hash: str) -> bool:
        return self.rehash or super().needs_rehash(password_hash)


@pytest.mark.parametrize("rehash", [False, True], ids=["plain", "rehash"])
def test_login_racing_a_reset_gets_no_session(
    cli: CliRunner, db: Session, db_url: str, rehash: bool
) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    racing = ResetDuringVerify(cli, rehash)
    app = create_app(cli.settings, secret_box=SecretBox(TEST_MASTER_KEY), passwords=racing)
    with TestClient(app, base_url="https://testserver") as browser:
        r = browser.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
        assert racing.fired
        assert r.status_code == 401
        assert browser.get("/api/v1/me").status_code == 401

    db.expire_all()
    user = get_user(db, ADMIN)
    assert db.scalars(select(UserSession)).all() == []
    # The CLI's hash survived: no rehash of the old password overwrote it.
    assert FAST_HASHER.verify(user.password_hash, NEW_PASSWORD)
    assert user.last_login_at is None
    assert [r.action for r in audit_rows(db)] == ["update", "login_failed"]
    with make_client(db_url) as browser:
        login(browser, ADMIN, NEW_PASSWORD)


def test_password_stdin_refused_on_a_terminal(cli: CliRunner, db: Session) -> None:
    user = add_user(db, ADMIN, Role.ADMIN)
    old_hash = user.password_hash
    stdin = FakeTty(NEW_PASSWORD + "\n")
    err = io.StringIO()
    console = Console(stdin=stdin, stdout=io.StringIO(), stderr=err, getpass=Prompt())
    code = main(
        ["reset-password", ADMIN, "--password-stdin"],
        console=console,
        settings=cli.settings,
        passwords=cli.passwords,
    )
    assert code == 1
    assert "echo" in err.getvalue() and "-T" in err.getvalue()
    assert stdin.tell() == 0  # nothing was read
    assert get_user(db, ADMIN).password_hash == old_hash


def test_non_utf8_stdin_gives_a_clean_error(cli: CliRunner, db: Session) -> None:
    add_user(db, ADMIN, Role.ADMIN)
    stdin = io.TextIOWrapper(io.BytesIO(b"example-pass\xffword-123\n"), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["reset-password", ADMIN, "--password-stdin"],
        console=Console(stdin=stdin, stdout=out, stderr=err),
        settings=cli.settings,
        passwords=cli.passwords,
    )
    assert code == 1
    assert "UTF-8" in err.getvalue()
    for leak in ("0xff", "\\xff", "position", "example-pass"):
        assert leak not in err.getvalue() + out.getvalue()
    assert audit_rows(db) == []


def test_viewer_without_admin_warns(cli: CliRunner) -> None:
    run = cli("create-user", "viewer1", "--role", "viewer", "--password-stdin", stdin=PASSWORD)
    assert run.code == 0, run.err
    assert "no admin user" in run.err

    run = cli("create-user", ADMIN, "--role", "admin", "--password-stdin", stdin=PASSWORD)
    assert run.code == 0 and run.err == ""
    run = cli("create-user", "viewer2", "--role", "viewer", "--password-stdin", stdin=PASSWORD)
    assert run.code == 0 and run.err == ""
