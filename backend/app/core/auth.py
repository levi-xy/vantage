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
    expired, then keys still **under** their limit, most attempts left first (least
    recently used first among equals). A key at its limit (a lockout) is never dropped,
    so flooding the limiter with junk keys can't unlock it, and a key close to its limit
    is dropped last. One scan drops a batch (up to 5% of the table).

    If too few keys can be dropped, the attempt is refused (fail closed). The scan's
    result is cached: until the first lockout can end, attempts that need more slots
    than were droppable are refused without scanning again. succeeded() and reset()
    clear the cache. That keeps a full table from turning every refused attempt into a
    scan under the lock.

    What this still doesn't prevent: an attacker can push a near-limit key out (e.g. a
    username at 4 of 5), resetting its count, by filling the table with username keys
    that have as few attempts left. Unlocked client keys don't help them: a client key
    has more attempts left than such a username, so it is dropped first and even shields
    the target by taking the batch ahead of it. In simulation that takes about
    0.95 x max_keys x 4 attempts, about 38,000 at the default cap, spread over at least
    about 760 addresses (or IPv6 /64s; 38,000 / 50 per client) within one window. With
    a handful of addresses it never succeeds. Over IPv6 one /48 holds 65,536 separate
    /64s, so the address floor is easy to meet there. Lockouts themselves are never
    lost. The complete fix is counters that aren't capped in memory (per user, in the
    database), which is a data-model change for a follow-up task.
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
        if max_keys < 2:
            raise ValueError("max_keys must be at least 2 (one login uses two keys)")
        # Cache of the last refusing scan: until _full_until (clock value), at most
        # _spare keys can be dropped (fewer if one of them is the attempt's own key).
        # Cleared whenever the number could have grown: a newly tracked key (it is
        # droppable), succeeded() and reset(). Otherwise only a lockout ending can raise
        # it, and none ends before _full_until; more failures on tracked keys only
        # lower it. So the cache never refuses an attempt that would fit.
        self._full_until = 0.0
        self._spare = 0

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
        most attempts left (limit minus recent failures), then least recently used. One
        scan drops a batch (up to 5% of the table), so a spray of new keys doesn't
        rescan the table every time. Expects ``_recent`` to have pruned ``keys`` already.
        """
        own = set(keys)
        if len(own) > self._max_keys:
            raise ValueError("more keys in one attempt than the limiter can track")
        needed = len(self._keys) + sum(1 for k in own if k not in self._keys) - self._max_keys
        if needed <= 0:
            return 0.0
        if now < self._full_until:
            # This attempt's own tracked keys under their limit can't be dropped for it.
            own_open = sum(
                1 for k in own if k in self._keys and len(self._keys[k].failures) < self._limit(k)
            )
            if needed > self._spare - own_open:
                return self._full_until - now
        candidates: list[tuple[int, int, int, str]] = []
        # When the first lockout can end (its oldest counted failure leaves the window).
        # Starts at a full window: nothing stays locked longer.
        next_change = now + self._window
        spare = 0
        for order, (key, window) in enumerate(self._keys.items()):
            recent = [t for t in window.failures if now - t < self._window]
            limit = self._limit(key)
            if len(recent) >= limit:
                next_change = min(next_change, recent[-limit] + self._window)
                continue
            spare += 1
            if key not in own:
                # Expired first, then most attempts left (as negative: smallest first).
                candidates.append((1 if recent else 0, len(recent) - limit, order, key))
        if len(candidates) < needed:
            self._full_until, self._spare = next_change, spare
            return max(next_change - now, 1.0)
        self._full_until = 0.0
        batch = max(needed, self._max_keys // 20)
        for *_, key in heapq.nsmallest(batch, candidates):
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
            window = self._keys.get(key)
            if window is None:
                window = self._keys[key] = _Window()
                self._full_until = 0.0  # one more droppable key: the cache may be stale
            window.failures.append(now)
            self._keys.move_to_end(key)
        return 0.0

    def retry_after(self, keys: Iterable[str]) -> int | None:
        """Seconds until a login may be tried again, or ``None`` if it's allowed now."""
        with self._lock:
            return self._seconds(self._wait(keys, self._clock()))

    def record_failure(self, keys: Iterable[str]) -> None:
        """Count a failure (dropped if there's no room for it; see the class doc)."""
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
