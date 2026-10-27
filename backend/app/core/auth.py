"""Passwords, session tokens and login rate limiting (M1.3).

Session tokens are random strings kept only in the user's httpOnly cookie. The database
stores a SHA-256 hash of each token (``session_id``), so a stolen database can't be used
to log in. The CSRF token is a second, independent hash of the same token: the browser
app can read it from ``/me``, but a cross-site page can't, because it can't read the cookie.
"""

import hashlib
import heapq
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
    own limit. At most ``max_keys`` keys are tracked, so random usernames and addresses
    can't use up memory.

    When the cap is reached, room is made by dropping keys whose failures have all
    expired, then keys still **under** their limit, fewest recent failures first (least
    recently used first among equals). A key at its limit (a lockout) is never dropped,
    so flooding the limiter with junk keys can't unlock it, and a key close to its limit
    is dropped last. If every tracked key is locked, there is no room and the attempt is
    refused (fail closed) until the first lockout can change; that answer is cached, so
    refusing costs no table scan.

    What this still doesn't prevent: an attacker who fills the whole table with keys
    that each have at least as many failures as a near-limit key (e.g. 10,000 usernames
    with 4 failures each, about 40,000 attempts from 800+ addresses per window) can
    still push that key out and reset its count. Lockouts themselves are never lost. A
    complete fix needs counters that aren't capped in memory (e.g. per user in the
    database), which is a data-model change.
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
        # Set when the table was full of keys that can't be dropped: no new key fits
        # before this time (clock value), unless succeeded() or reset() frees a slot.
        self._full_until = 0.0

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
                # Keep locked keys at the recent end, so eviction scans reach them last.
                self._keys.move_to_end(key)
        return wait

    @staticmethod
    def _seconds(wait: float) -> int | None:
        return max(1, int(wait) + 1) if wait > 0 else None

    def _make_room(self, keys: list[str], now: float) -> float:
        """Free space for ``keys``. Returns 0 if they fit, else seconds until one might.

        Only expired keys and keys under their limit are dropped: expired first, then
        fewest recent failures, then least recently used. One scan drops a batch (up to
        5% of the table), so a spray of new keys doesn't rescan the table every time.
        """
        needed = len(self._keys) + sum(1 for k in set(keys) if k not in self._keys)
        needed -= self._max_keys
        if needed <= 0:
            return 0.0
        if now < self._full_until:
            return self._full_until - now
        candidates: list[tuple[int, int, str]] = []
        # When the first tracked key could become droppable (its oldest counted failure
        # leaves the window). Starts at a full window: nothing can stay locked longer.
        next_change = now + self._window
        protected_open = False
        for order, (key, window) in enumerate(self._keys.items()):
            recent = [t for t in window.failures if now - t < self._window]
            limit = self._limit(key)
            if len(recent) >= limit:
                next_change = min(next_change, recent[-limit] + self._window)
            elif key in keys:
                protected_open = True  # in use by this attempt, but another may drop it
            else:
                candidates.append((len(recent), order, key))
        if len(candidates) < needed:
            if not candidates and not protected_open:
                # Every tracked key is locked: nothing can change before next_change
                # except succeeded()/reset(), which clear this.
                self._full_until = next_change
            return max(next_change - now, 1.0)
        batch = max(needed, self._max_keys // 20)
        for _, _, key in heapq.nsmallest(batch, candidates):
            del self._keys[key]
        return 0.0

    def _record(self, keys: list[str], now: float) -> float:
        """Count a failure on every key. Returns 0, or seconds to wait if there's no room."""
        for key in keys:
            self._recent(key, now)
        wait = self._make_room(keys, now)
        if wait > 0:
            return wait
        for key in keys:
            window = self._keys.setdefault(key, _Window())
            window.failures.append(now)
            self._keys.move_to_end(key)
        return 0.0

    def retry_after(self, keys: Iterable[str]) -> int | None:
        """Seconds until a login may be tried again, or ``None`` if it's allowed now."""
        with self._lock:
            return self._seconds(self._wait(keys, self._clock()))

    def record_failure(self, keys: Iterable[str]) -> None:
        """Count a failure (dropped if every tracked key is locked; see the class doc)."""
        with self._lock:
            self._record(list(keys), self._clock())

    def acquire(self, keys: list[str]) -> int | None:
        """Check the limit and count this attempt as a failure, in one atomic step.

        Counting before the (slow) password check means parallel requests can't all
        pass the check before any failure is recorded. Call ``succeeded`` if it works.
        Returns seconds to wait if the attempt is refused (and then counts nothing): the
        keys are locked, or the limiter is full of locked keys and can't track them.
        """
        with self._lock:
            now = self._clock()
            wait = self._wait(keys, now) or self._record(keys, now)
            return self._seconds(wait)

    def succeeded(self, user_key: str, other_keys: Iterable[str]) -> None:
        """Undo ``acquire`` for a successful login: clear the user's failures, refund one
        attempt on the other keys."""
        with self._lock:
            self._full_until = 0.0  # may have freed a slot
            self._keys.pop(user_key, None)
            for key in other_keys:
                window = self._keys.get(key)
                if window is not None and window.failures:
                    window.failures.pop()

    def reset(self, key: str) -> None:
        with self._lock:
            self._full_until = 0.0
            self._keys.pop(key, None)
