"""Admin CLI (M1.6): user recovery on the server, without logging in.

Run it where the backend runs::

    docker compose exec backend python -m app.cli reset-password <username>
    docker compose exec backend python -m app.cli create-user <username> --role admin
    docker compose exec backend python -m app.cli list-users
    docker compose exec backend python -m app.cli unlock <username>

Shell access to the host is the trust boundary, so no login is needed.

What it loads, and what it doesn't:

* The app's own settings (``load_settings``), database engine and session factory,
  Argon2 hasher (``Passwords``, the same parameters as login) and audit writer.
* **Not the master key.** The CLI only touches ``users``, ``sessions``,
  ``login_failures``, ``settings`` (the setup marker) and ``audit_log``, none of which
  hold encrypted data, so it has nothing to encrypt or decrypt. The master key check
  exists so the app never stores secrets without encryption; the CLI stores no
  secrets, so it skips nothing by not loading it. Not needing the key also means a
  broken key file doesn't block recovery.
* Not the web app, the poll scheduler or the collector: ``app.main`` is never imported.
* It never creates or migrates a database. A SQLite file that doesn't exist, or a
  database without the tables, is an error (the backend applies migrations on start).

Passwords come from a hidden prompt (asked twice) or, with ``--password-stdin``, one
line of stdin. Never from an argument or an environment variable. They are never
printed, logged or audited: the audit entry records the password as ``"<changed>"``.
Error messages never repeat what was typed, since a mistyped command may hold a password.
"""

import argparse
import getpass as getpass_module
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import CursorResult, Engine, delete, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, SETUP_MARKER, USERNAME_PATTERN
from app.core import audit
from app.core.auth import Passwords
from app.core.config import ConfigError, Settings, load_settings
from app.core.db import make_engine, make_session_factory
from app.core.login_failures import UserLoginFailures
from app.models import Role, Setting, User, UserSession

# Recorded in the audit entry's ``changes`` so CLI actions can be told apart from API
# ones. ``user_id`` (the actor) is empty: the operator is whoever has a shell on the host.
AUDIT_SOURCE = "cli"
_REQUIRED_TABLES = ("users", "sessions", "login_failures", "settings", "audit_log")

# Printed after reset-password and unlock, which clear the user's failed-login count
# (kept in the database). The per-client limit (failures from one address) is in the
# backend's memory, so only a restart clears that.
LIMITER_HINT = (
    "The user's failed-login count was cleared. If logins from one address are still "
    "refused (too many failures from that address), restart the backend to clear the "
    "per-address limit: docker compose restart backend"
)


class CliError(Exception):
    """A failure to report to the operator. The message never contains a secret."""


# --- validation: the same rules as the setup wizard / login API -------------------------


class _NewUsername(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=USERNAME_PATTERN)


class _NewPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


def _validate_username(username: str) -> str:
    try:
        return _NewUsername(username=username).username
    except ValidationError:
        # Not pydantic's message: it would repeat the input.
        raise CliError(
            "invalid username: 1 to 64 characters, letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        ) from None


def _validate_password(password: str) -> None:
    try:
        _NewPassword(password=SecretStr(password))
    except ValidationError:
        raise CliError(
            f"the password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long"
        ) from None


# --- input and output ----------------------------------------------------------------


@dataclass
class Console:
    """Where the CLI reads and writes. Tests swap these out."""

    stdin: IO[str] = field(default_factory=lambda: sys.stdin)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)
    getpass: Callable[[str], str] = getpass_module.getpass

    def out(self, line: str) -> None:
        print(line, file=self.stdout)

    def err(self, line: str) -> None:
        print(line, file=self.stderr)


def _read_password(console: Console, from_stdin: bool) -> str:
    """The new password: one stdin line, or a hidden prompt asked twice."""
    try:
        return _read_password_unchecked(console, from_stdin)
    except (UnicodeError, OSError):
        # Not the exception text: a decode error quotes the offending byte and position.
        raise CliError(
            "could not read the password (stdin must be UTF-8 text); nothing was changed"
        ) from None


def _read_password_unchecked(console: Console, from_stdin: bool) -> str:
    if from_stdin:
        if console.stdin.isatty():
            # A terminal echoes what is typed into stdin. `docker compose exec` gives one
            # by default; piping needs `exec -T`.
            raise CliError(
                "--password-stdin needs piped input, not a terminal (it would echo the "
                "password): pipe it in with docker compose exec -T, or leave out "
                "--password-stdin to get a hidden prompt"
            )
        # One line. Read a little past the limit so an over-long line is still refused
        # rather than silently cut short.
        line = console.stdin.readline(MAX_PASSWORD_LENGTH + 2)
        if line.endswith("\n"):
            line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
        return line
    if not console.stdin.isatty():
        # getpass would fall back to reading stdin with echo on; refuse instead.
        raise CliError(
            "no terminal to prompt for the password on: run with a terminal "
            "(docker compose exec without -T) or pipe it in with --password-stdin"
        )
    first = console.getpass("New password: ")
    second = console.getpass("Repeat new password: ")
    if not secrets.compare_digest(first.encode(), second.encode()):
        raise CliError("the passwords don't match; nothing was changed")
    return first


# --- database ------------------------------------------------------------------------


def _open_engine(settings: Settings) -> Engine:
    """The configured database, refusing to create or use one that isn't set up.

    Messages never include the database URL: it may contain a password.
    """
    try:
        url = make_url(settings.database_url)
    except ArgumentError:
        raise CliError("VANTAGE_DATABASE_URL is not a valid database URL") from None
    if url.get_backend_name() == "sqlite":
        # Connecting to a missing SQLite file would create an empty one.
        path = url.database
        if not path or path == ":memory:" or path.startswith("file:"):
            raise CliError("VANTAGE_DATABASE_URL must point at the backend's SQLite file")
        if not Path(path).is_file():
            raise CliError(
                "database file not found: check VANTAGE_DATABASE_URL (inside the backend "
                "container it is set for you) and start the backend once to create it"
            )
    engine = make_engine(settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    except SQLAlchemyError:
        engine.dispose()
        raise CliError("could not open the database: check VANTAGE_DATABASE_URL") from None
    if not tables.issuperset(_REQUIRED_TABLES):
        engine.dispose()
        raise CliError(
            "the database has no Vantage tables yet: start the backend once (it applies "
            "the migrations), then try again"
        )
    return engine


@dataclass
class Context:
    console: Console
    session_factory: sessionmaker[Session]
    passwords: Passwords


def _find_user(db: Session, username: str) -> User | None:
    # Exact match, like login (usernames are stored as typed and compared as is).
    return db.scalar(select(User).where(User.username == username))


def _no_such_user() -> CliError:
    # The typed name isn't repeated: a mistyped command may have a password there.
    return CliError("no user with that username (usernames are case-sensitive; see list-users)")


# --- commands ------------------------------------------------------------------------


def reset_password(ctx: Context, username: str, from_stdin: bool) -> None:
    with ctx.session_factory() as db:
        if _find_user(db, username) is None:
            raise _no_such_user()
    password = _read_password(ctx.console, from_stdin)
    _validate_password(password)
    password_hash = ctx.passwords.hash(password)
    del password

    with ctx.session_factory() as db:
        user = _find_user(db, username)
        if user is None:  # deleted while the operator was typing
            raise _no_such_user()
        user.password_hash = password_hash
        # The user row is written (and locked) before the sessions are deleted. A login
        # only creates a session if it can update the row while the hash is still the
        # one it verified (see app.api.auth.login), so either that login fails, or it
        # commits first and its session is deleted here.
        db.flush()
        # Every session ends, so old cookies stop working on their next request.
        ended = cast(
            "CursorResult[Any]",
            db.execute(delete(UserSession).where(UserSession.user_id == user.id)),
        ).rowcount
        # The failures counted so far were guesses at the old password: they say nothing
        # about the new one, and keeping them would leave the user locked out after the
        # reset that was meant to let them back in.
        cleared = UserLoginFailures.clear(db, user.id)
        audit.record(
            db,
            action=audit.Action.UPDATE,
            target_type="user",
            target_id=user.id,
            user_id=None,
            # The password itself is never passed in; the key is redacted regardless.
            changes={
                "password": audit.REDACTED,
                "sessions_ended": ended,
                "failed_logins_cleared": cleared,
                "source": AUDIT_SOURCE,
            },
        )
        db.commit()
        ctx.console.out(
            f"Password reset for {user.username!r}; ended {ended} session(s), "
            f"cleared {cleared} failed login(s)."
        )
    ctx.console.out(LIMITER_HINT)


def unlock(ctx: Context, username: str) -> None:
    """Clear the user's failed-login count, so a locked-out user can log in at once."""
    with ctx.session_factory() as db:
        user = _find_user(db, username)
        if user is None:
            raise _no_such_user()
        cleared = UserLoginFailures.clear(db, user.id)
        audit.record(
            db,
            action=audit.Action.UPDATE,
            target_type="user",
            target_id=user.id,
            user_id=None,
            changes={"failed_logins_cleared": cleared, "source": AUDIT_SOURCE},
        )
        db.commit()
        ctx.console.out(f"Unlocked {user.username!r}; cleared {cleared} failed login(s).")
    ctx.console.out(LIMITER_HINT)


def create_user(ctx: Context, username: str, role: Role, from_stdin: bool) -> None:
    username = _validate_username(username)

    def exists(db: Session) -> bool:
        # Case-insensitive: "Admin" and "admin" look alike to people, and a failed login
        # for a name that differs only in case counts on the existing account.
        found = db.scalar(select(User.id).where(func.lower(User.username) == username.lower()))
        return found is not None

    with ctx.session_factory() as db:
        if exists(db):
            raise CliError("a user with that username already exists")
    password = _read_password(ctx.console, from_stdin)
    _validate_password(password)
    password_hash = ctx.passwords.hash(password)
    del password

    with ctx.session_factory() as db:
        if exists(db):
            raise CliError("a user with that username already exists")
        user = User(username=username, password_hash=password_hash, role=role)
        db.add(user)
        # Like setup: once a user exists, network setup stays closed even if every user
        # is later deleted.
        if db.get(Setting, SETUP_MARKER) is None:
            db.add(Setting(key=SETUP_MARKER, value=True))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise CliError("a user with that username already exists") from None
        audit.record(
            db,
            action=audit.Action.CREATE,
            target_type="user",
            target_id=user.id,
            user_id=None,
            changes={
                "username": user.username,
                "role": user.role,
                "password": audit.REDACTED,
                "source": AUDIT_SOURCE,
            },
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise CliError("a user with that username already exists") from None
        ctx.console.out(f"Created {role.value} {user.username!r} (id {user.id}).")
        has_admin = db.scalar(select(User.id).where(User.role == Role.ADMIN).limit(1))
        if has_admin is None:
            ctx.console.err(
                "warning: there is no admin user, and setup in the browser is now closed. "
                "Create one with: python -m app.cli create-user <username> --role admin"
            )


def list_users(ctx: Context) -> None:
    with ctx.session_factory() as db:
        # Only these columns are loaded, so the hash never leaves the database.
        rows = db.execute(
            select(User.id, User.username, User.role, User.created_at, User.last_login_at).order_by(
                User.id
            )
        ).all()
    header = ("ID", "USERNAME", "ROLE", "CREATED_AT", "LAST_LOGIN_AT")
    table = [header] + [
        (
            str(row.id),
            row.username,
            Role(row.role).value,
            row.created_at.isoformat(timespec="seconds"),
            row.last_login_at.isoformat(timespec="seconds") if row.last_login_at else "never",
        )
        for row in rows
    ]
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    for r in table:
        ctx.console.out(
            "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
        )


# --- argument parsing ----------------------------------------------------------------

# argparse repeats the offending value in these messages; a mistyped command may carry a
# password there (``--password hunter2``), so the value is cut off.
_ECHOING_ERRORS = ("unrecognized arguments", "invalid choice", "ignored explicit argument")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        for marker in _ECHOING_ERRORS:
            index = message.find(marker)
            if index >= 0:
                message = message[: index + len(marker)] + " (not shown; see --help)"
                if marker == "unrecognized arguments":
                    message += (
                        ". Passwords are never taken as arguments: use the prompt or "
                        "--password-stdin"
                    )
                break
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False everywhere: otherwise "--password" would be taken as an
    # abbreviation of "--password-stdin".
    parser = _Parser(
        prog="python -m app.cli",
        description="Vantage admin tasks. Run on the server; no login needed.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    stdin_help = "read the new password from one line of stdin instead of prompting"

    reset = commands.add_parser(
        "reset-password",
        help="set a new password and end the user's sessions",
        description="Set a new password for a user and end all of their sessions.",
        allow_abbrev=False,
    )
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true", help=stdin_help)

    create = commands.add_parser(
        "create-user",
        help="create a user",
        description="Create a user.",
        allow_abbrev=False,
    )
    create.add_argument("username")
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument("--password-stdin", action="store_true", help=stdin_help)

    commands.add_parser(
        "list-users",
        help="list users (never shows password hashes)",
        description="List users.",
        allow_abbrev=False,
    )

    unlock_cmd = commands.add_parser(
        "unlock",
        help="clear a user's failed logins so they can log in again now",
        description=(
            "Clear a user's failed-login count (the per-user login lockout). The "
            "per-address limit lives in the backend's memory: a restart clears it."
        ),
        allow_abbrev=False,
    )
    unlock_cmd.add_argument("username")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    console: Console | None = None,
    settings: Settings | None = None,
    passwords: Passwords | None = None,
) -> int:
    """Run one command. Returns the exit code: 0 on success, 1 on failure, 2 on bad usage."""
    console = console or Console()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # usage errors and --help
        return exc.code if isinstance(exc.code, int) else 2
    engine: Engine | None = None
    try:
        settings = settings or load_settings()
        engine = _open_engine(settings)
        ctx = Context(
            console=console,
            session_factory=make_session_factory(engine),
            passwords=passwords or Passwords(),
        )
        if args.command == "reset-password":
            reset_password(ctx, args.username, args.password_stdin)
        elif args.command == "create-user":
            create_user(ctx, args.username, Role(args.role), args.password_stdin)
        elif args.command == "unlock":
            unlock(ctx, args.username)
        else:
            list_users(ctx)
    except (CliError, ConfigError) as exc:  # ConfigError names the variable, never its value
        console.err(f"error: {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        console.err("cancelled; nothing was changed")
        return 1
    except (UnicodeError, OSError):
        # Not the exception text: a decode error quotes the offending byte.
        console.err("error: reading input or writing output failed")
        return 1
    except SQLAlchemyError:
        # Not the exception text: it can include the URL or row values.
        console.err("error: the database refused the change; nothing was changed")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0
