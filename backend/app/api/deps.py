"""Dependencies shared by the API routers: database session, current user, roles, CSRF."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth import LoginRateLimiter, Passwords, csrf_matches, session_id
from app.core.client_address import CLIENT_ADDRESS_HEADER, resolve_client_address
from app.core.config import Settings
from app.core.db import session_scope, utcnow
from app.models import Role, User, UserSession

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
# last_seen_at is written at most this often, not on every request.
_LAST_SEEN_RESOLUTION = timedelta(seconds=60)
_MAX_TOKEN_LENGTH = 128


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_passwords(request: Request) -> Passwords:
    passwords: Passwords = request.app.state.passwords
    return passwords


def get_login_limiter(request: Request) -> LoginRateLimiter:
    limiter: LoginRateLimiter = request.app.state.login_limiter
    return limiter


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


def get_client_address(request: Request, settings: SettingsDep) -> str:
    """The real client's address: the TCP peer, or X-Real-IP when the peer is a trusted proxy.

    The one place the app derives a client address; use this (via ClientAddressDep)
    wherever an address is needed so every consumer agrees.
    """
    return resolve_client_address(
        request.client.host if request.client else None,
        request.headers.getlist(CLIENT_ADDRESS_HEADER),
        settings.trusted_proxies,
    )


ClientAddressDep = Annotated[str, Depends(get_client_address)]


# --- session cookie ----------------------------------------------------------------


def cookie_name(settings: Settings) -> str:
    # The __Host- prefix makes browsers refuse the cookie unless it is Secure, has Path=/
    # and no Domain, so another host on the network can't plant or overwrite it.
    return "__Host-vantage_session" if settings.cookie_secure else "vantage_session"


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=settings.session_max_hours * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def session_token(request: Request, settings: Settings) -> str | None:
    token = request.cookies.get(cookie_name(settings))
    if not token or len(token) > _MAX_TOKEN_LENGTH:
        return None
    return token


# --- current user ------------------------------------------------------------------


@dataclass
class Authenticated:
    user: User
    session: UserSession
    token: str


def _unauthenticated() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")


def authenticated(request: Request, db: DbDep, settings: SettingsDep) -> Authenticated:
    """The logged-in user. Also checks the CSRF token on state-changing requests."""
    token = session_token(request, settings)
    if token is None:
        raise _unauthenticated()
    row = db.get(UserSession, session_id(token))
    if row is None:
        raise _unauthenticated()

    now = utcnow()
    idle = timedelta(minutes=settings.session_idle_minutes)
    if row.expires_at <= now or now - row.last_seen_at > idle:
        db.delete(row)
        db.commit()
        raise _unauthenticated()

    if request.method not in SAFE_METHODS and not csrf_matches(
        token, request.headers.get(CSRF_HEADER)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")

    user = db.get(User, row.user_id)
    if user is None:  # unreachable: sessions are deleted with their user
        raise _unauthenticated()
    if now - row.last_seen_at >= _LAST_SEEN_RESOLUTION:
        row.last_seen_at = now
    return Authenticated(user=user, session=row, token=token)


AuthDep = Annotated[Authenticated, Depends(authenticated)]


def require_admin(auth: AuthDep) -> Authenticated:
    if auth.user.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return auth


AdminDep = Annotated[Authenticated, Depends(require_admin)]


def require_json(request: Request) -> None:
    """For endpoints that work without a session (setup, login).

    A cross-site form can't send ``application/json`` without a CORS preflight, and the
    backend allows no cross-origin requests, so this blocks login CSRF.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected application/json")
