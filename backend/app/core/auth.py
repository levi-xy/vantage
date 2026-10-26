"""Passwords, session tokens and login rate limiting (M1.3).

Session tokens are random strings kept only in the user's httpOnly cookie. The database
stores a SHA-256 hash of each token (``session_id``), so a stolen database can't be used
to log in. The CSRF token is a second, independent hash of the same token: the browser
app can read it from ``/me``, but a cross-site page can't, because it can't read the cookie.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_TOKEN_BYTES = 32


# --- passwords -----------------------------------------------------------------


class Passwords:
    """Argon2id hashing (argon2-cffi's RFC 9106 defaults) with constant-ish timing."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()
        # Verified against when the username doesn't exist, so a login for an unknown
        # user takes as long as one for a real user.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash or self._dummy_hash, password) and (
                password_hash is not None
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


# --- session and CSRF tokens -------------------------------------------------------


def new_session_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def session_id(token: str) -> str:
    """What the database stores for a session token."""
    return hashlib.sha256(b"vantage/session/v1\x00" + token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    """The CSRF token for a session. Independent of ``session_id`` (different prefix)."""
    return hashlib.sha256(b"vantage/csrf/v1\x00" + token.encode()).hexdigest()


def csrf_matches(token: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(token).encode(), presented.encode())


# --- login rate limit ------------------------------------------------------------


@dataclass
class _Window:
    failures: list[float] = field(default_factory=list)


class LoginRateLimiter:
    """Counts failed logins per key over a sliding window. In memory, per process.

    Keys are e.g. ``"user:alice"`` and ``"client:192.0.2.5"``. Each key prefix has its
    own limit. The number of tracked keys is capped so random usernames can't use up memory.
    """

    def __init__(
        self,
        limits: dict[str, int],
        window_seconds: float = 900.0,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._keys: OrderedDict[str, _Window] = OrderedDict()
        self._lock = threading.Lock()

    def _limit(self, key: str) -> int:
        return self._limits[key.split(":", 1)[0]]

    def _recent(self, key: str, now: float) -> list[float]:
        window = self._keys.get(key)
        if window is None:
            return []
        window.failures = [t for t in window.failures if now - t < self._window]
        if not window.failures:
            del self._keys[key]
            return []
        return window.failures

    def _wait(self, keys: Iterable[str], now: float) -> float:
        wait = 0.0
        for key in keys:
            failures = self._recent(key, now)
            limit = self._limit(key)
            if len(failures) >= limit:
                wait = max(wait, failures[-limit] + self._window - now)
                # Keep a locked key fresh so eviction under the key cap can't unlock it.
                self._keys.move_to_end(key)
        return wait

    @staticmethod
    def _seconds(wait: float) -> int | None:
        return max(1, int(wait) + 1) if wait > 0 else None

    def _record(self, keys: Iterable[str], now: float) -> None:
        for key in keys:
            self._recent(key, now)
            window = self._keys.setdefault(key, _Window())
            window.failures.append(now)
            self._keys.move_to_end(key)
        while len(self._keys) > self._max_keys:
            self._keys.popitem(last=False)

    def retry_after(self, keys: Iterable[str]) -> int | None:
        """Seconds until a login may be tried again, or ``None`` if it's allowed now."""
        with self._lock:
            return self._seconds(self._wait(keys, self._clock()))

    def record_failure(self, keys: Iterable[str]) -> None:
        with self._lock:
            self._record(keys, self._clock())

    def acquire(self, keys: list[str]) -> int | None:
        """Check the limit and count this attempt as a failure, in one atomic step.

        Counting before the (slow) password check means parallel requests can't all
        pass the check before any failure is recorded. Call ``succeeded`` if it works.
        Returns seconds to wait if the attempt is refused (and then counts nothing).
        """
        with self._lock:
            now = self._clock()
            wait = self._wait(keys, now)
            if wait > 0:
                return self._seconds(wait)
            self._record(keys, now)
            return None

    def succeeded(self, user_key: str, other_keys: Iterable[str]) -> None:
        """Undo ``acquire`` for a successful login: clear the user's failures, refund one
        attempt on the other keys."""
        with self._lock:
            self._keys.pop(user_key, None)
            for key in other_keys:
                window = self._keys.get(key)
                if window is not None and window.failures:
                    window.failures.pop()

    def reset(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)
