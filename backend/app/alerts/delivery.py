"""Durable alert notification delivery: the outbox and its dispatcher (P0-5).

The queue is the ``alert_notifications`` table. :func:`enqueue` adds one row per
(alert change, channel) inside the caller's transaction, so a change and its messages
commit or roll back together, and nothing is sent before the change is stored. The
evaluator and the rule API only insert rows; they never wait for a send.

One :class:`DeliveryDispatcher` per backend sends them:

* **Claiming.** Per channel only the oldest unfinished row (the *head*) can be claimed,
  so messages arrive in order and a RESOLVED never overtakes its FIRING. A claim is a
  conditional update ``pending → sending`` (with ``claim_token``, ``claimed_at`` and the
  attempt counted); on Postgres the select uses ``FOR UPDATE SKIP LOCKED``.
* **Batching.** The head carries every later pending row of its channel: one message of up
  to :data:`BATCH_LINES` lines, then "and N more, see Alerts". On success the head is
  ``delivered`` and the rows it carried are ``folded`` into it.
* **Concurrency.** At most :data:`MAX_IN_FLIGHT` sends at once, one per channel.
* **Retries.** After a failed attempt: 10 s, 30 s, 1 min, 2 min, 5 min, then every 10 min,
  until the row is 24 h old (then ``failed``, ``expired``). A Telegram 429 waits its
  ``retry_after`` instead (at most 10 min). Permanent errors (Telegram 400/401/403/404,
  channel credentials that can't be decrypted) fail at once.
* **Overflow.** A channel with :data:`OVERFLOW_LIMIT` unfinished rows stores new rows as
  ``folded`` into one pending summary row that counts them. They stay unfinished
  (``finished_at`` empty) until the summary finishes, then share its outcome.
* **Shared causes (P0-4).** A message the evaluator folds into another one (an alert's NO
  DATA carried by a system alert or by its device's instance-down message) is stored
  ``folded`` into that *carrier* (:func:`fold_into`) on the same channel, and takes the
  carrier's outcome like an overflow row.
* **Restart.** At start every row left ``sending`` goes back to ``pending`` (its attempt
  stays counted). Delivery is at least once: a crash between Telegram accepting a message
  and the row being marked can send it twice; it can't lose it.

If the dispatcher doesn't run, rows wait. ``error_kind`` is always a fixed code, never a
response text or a token; log lines carry ids, counts and that code only.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from prometheus_client.metrics_core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.channels import (
    MAX_TEXT_LENGTH,
    ClientFactory,
    SendError,
    cap_text,
    default_client_factory,
    deliver,
    load_config,
)
from app.core.crypto import DecryptionError, SecretBox
from app.core.db import utcnow
from app.models import (
    AlertEvent,
    AlertNotification,
    AlertState,
    Channel,
    ChannelType,
    NotificationKind,
    NotificationStatus,
    SystemKind,
)
from app.models.notification import UNFINISHED

log = logging.getLogger(__name__)

N = AlertNotification
Kind = NotificationKind
Status = NotificationStatus

MAX_IN_FLIGHT = 4
BATCH_LINES = 20
OVERFLOW_LIMIT = 500
POLL_SECONDS = 1.0
# Upper bound on one send, on top of the guard's own per-request timeouts.
SEND_TIMEOUT_SECONDS = 30.0
MAX_AGE = timedelta(hours=24)
BACKOFF_SECONDS = (10, 30, 60, 120, 300)
MAX_WAIT_SECONDS = 600
# A row still "sending" this long after its claim lost its sender (it went back to pending).
STALE_CLAIM = timedelta(minutes=5)
# Undelivered this long: the UI shows a banner.
STUCK_AFTER = timedelta(minutes=15)
# A failed row keeps the banner up this long after it failed.
FAILED_BANNER_WINDOW = timedelta(hours=24)

_CHANNEL_NAMES = {ChannelType.TELEGRAM: "Telegram"}


def backoff_seconds(attempts: int) -> int:
    """The wait after the ``attempts``-th failed attempt (1-based)."""
    if attempts < 1:
        return BACKOFF_SECONDS[0]
    if attempts <= len(BACKOFF_SECONDS):
        return BACKOFF_SECONDS[attempts - 1]
    return MAX_WAIT_SECONDS


# --- the outbox --------------------------------------------------------------------------


def _unfinished_counts(db: Session, channel_ids: Iterable[int]) -> dict[int, int]:
    """Unfinished rows per channel, the overflow summary itself not counted."""
    rows = db.execute(
        select(N.channel_id, func.count())
        .where(
            N.channel_id.in_(list(channel_ids)),
            N.status.in_(UNFINISHED),
            N.kind != Kind.SUMMARY,
        )
        .group_by(N.channel_id)
    )
    return {cid: n for cid, n in rows if cid is not None}


def _summary_text(db: Session, summary_id: int, channel: Channel) -> str:
    counts = dict(
        db.execute(
            select(N.kind, func.count()).where(N.folded_into == summary_id).group_by(N.kind)
        ).all()
    )
    total = sum(counts.values())
    parts = [
        f"{counts[k]} {label}"
        for k, label in (
            (Kind.FIRING, "firing"),
            (Kind.NO_DATA, "no data"),
            (Kind.RESOLVED, "resolved"),
            (Kind.RULE_CHANGE, "rule changes"),
        )
        if counts.get(k)
    ]
    where = _CHANNEL_NAMES.get(channel.type, "the channel")
    change = "alert change" if total == 1 else "alert changes"
    return f"{total} more {change} while {where} was unreachable: {', '.join(parts)}"


def _pending_summary(db: Session, channel_id: int) -> AlertNotification | None:
    return db.scalar(
        select(N)
        .where(N.channel_id == channel_id, N.kind == Kind.SUMMARY, N.status == Status.PENDING)
        .order_by(N.id.desc())
        .limit(1)
    )


def _fold_into_summary(db: Session, row: AlertNotification, channel: Channel) -> None:
    """Store ``row`` as carried by the channel's pending summary row (created if needed).

    The row stays unfinished (``finished_at`` empty) until the summary finishes; it then
    takes the summary's outcome (:func:`settle_overflow`). The summary's text is rewritten
    only while it is still pending: if the dispatcher claimed it meanwhile, the row goes
    into a new summary instead, so no message counts a change it never carried.
    """
    summary = _pending_summary(db, channel.id)
    while True:
        if summary is None:
            summary = N(
                channel_id=channel.id,
                kind=Kind.SUMMARY,
                text="",
                status=Status.PENDING,
                next_attempt_at=row.created_at,
                created_at=row.created_at,
            )
            db.add(summary)
            db.flush()
        row.status = Status.FOLDED
        row.folded_into = summary.id
        row.finished_at = None
        db.flush()
        text = cap_text(_summary_text(db, summary.id, channel))
        written = db.execute(
            update(N)
            .where(N.id == summary.id, N.status == Status.PENDING)
            .values(text=text)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount:  # type: ignore[attr-defined]
            return
        summary = None  # claimed meanwhile: start a new one


def fold_into(
    db: Session, row: AlertNotification, carrier: AlertNotification, now: datetime
) -> None:
    """Store ``row`` as carried by ``carrier`` (no commit). It takes the carrier's outcome:
    at once if the carrier has finished, otherwise when it does (:func:`settle_overflow`).

    A carrier that is itself folded and unfinished (queued in an overflow summary) is
    followed to the row that really carries it: only rows folded directly into a finished
    row are settled, so a row folded into a waiting folded row would never finish."""
    seen = {carrier.id}
    while (
        carrier.status == Status.FOLDED
        and carrier.finished_at is None
        and carrier.folded_into is not None
    ):
        parent = db.get(N, carrier.folded_into)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        carrier = parent
    row.status = Status.FOLDED
    row.folded_into = carrier.id
    row.finished_at = None
    if carrier.finished_at is not None:
        row.finished_at = now
        if carrier.status in (Status.FAILED, Status.CANCELLED):
            row.status = carrier.status
            row.error_kind = carrier.error_kind
    db.flush()


def settle_overflow(db: Session, carrier_ids: Iterable[int], now: datetime) -> int:
    """Give the rows carried by finished carrier rows their outcome (no commit): the rows
    of an overflow summary, and those folded in by the evaluator (:func:`fold_into`).

    Delivered (directly or in a batch): the rows stay ``folded`` and get ``finished_at``.
    Failed: ``failed`` with the summary's ``error_kind``. Cancelled: ``cancelled``.
    Returns how many rows ended ``failed``.
    """
    failed = 0
    carriers = db.execute(
        select(N.id, N.status, N.error_kind, N.finished_at).where(N.id.in_(list(carrier_ids)))
    )
    for carrier_id, status, error_kind, finished_at in carriers:
        if finished_at is None:
            continue  # still waiting: so do its rows
        carried = (
            (N.folded_into == carrier_id) & (N.status == Status.FOLDED) & N.finished_at.is_(None)
        )
        values: dict[str, object] = {"finished_at": now}
        if status == Status.FAILED:
            values.update(status=Status.FAILED, error_kind=error_kind)
        elif status == Status.CANCELLED:
            values.update(status=Status.CANCELLED)
        result = db.execute(
            update(N).where(carried).values(**values).execution_options(synchronize_session=False)
        )
        if status == Status.FAILED:
            failed += int(result.rowcount)  # type: ignore[attr-defined]
    return failed


def enqueue(
    db: Session,
    *,
    channel_ids: Sequence[int],
    kind: NotificationKind,
    text: str,
    event_id: int | None = None,
    now: datetime | None = None,
) -> list[AlertNotification]:
    """Queue ``text`` for each existing channel, in the caller's transaction (no commit).

    ``text`` must hold no secret; it is capped at Telegram's 4,096 characters here.
    Channel ids that no longer exist are skipped.
    """
    ids = sorted(set(channel_ids))
    if not ids:
        return []
    now = now or utcnow()
    channels = list(db.scalars(select(Channel).where(Channel.id.in_(ids)).order_by(Channel.id)))
    counts = _unfinished_counts(db, (c.id for c in channels))
    body = cap_text(text)
    rows = []
    for channel in channels:
        row = N(
            event_id=event_id,
            channel_id=channel.id,
            kind=kind,
            text=body,
            status=Status.PENDING,
            next_attempt_at=now,
            created_at=now,
        )
        db.add(row)
        if counts.get(channel.id, 0) >= OVERFLOW_LIMIT:
            db.flush()
            _fold_into_summary(db, row, channel)
        rows.append(row)
    db.flush()
    return rows


def cancel_for_channel(db: Session, channel_id: int, now: datetime | None = None) -> int:
    """Cancel the channel's unfinished rows, overflow rows included (no commit); call
    before deleting it."""
    result = db.execute(
        update(N)
        .where(
            N.channel_id == channel_id,
            N.status.in_(UNFINISHED) | ((N.status == Status.FOLDED) & N.finished_at.is_(None)),
        )
        .values(status=Status.CANCELLED, finished_at=now or utcnow(), claim_token=None)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def batch_text(texts: Sequence[str], weights: Sequence[int] | None = None) -> str:
    """One message for a batch: up to :data:`BATCH_LINES` lines, then "and N more".

    ``weights``: how many alert changes each row stands for (an overflow summary stands
    for every row it carries); "and N more" counts changes, not rows.
    """
    weights = list(weights) if weights is not None else [1] * len(texts)
    if len(texts) == 1:
        return cap_text(texts[0])
    budget = MAX_TEXT_LENGTH - 64  # room for the "and N more" line
    shown: list[str] = []
    used = 0
    for text in texts[:BATCH_LINES]:
        size = len(text) + 1
        if shown and used + size > budget:
            break
        shown.append(text)
        used += size
    rest = sum(weights[len(shown) :])
    if rest:
        shown.append(f"and {rest} more, see Alerts")
    return cap_text("\n".join(shown))


def stuck_count(db: Session, now: datetime | None = None) -> int:
    """Rows unfinished for more than :data:`STUCK_AFTER`, plus rows that failed within
    the last :data:`FAILED_BANNER_WINDOW` (counted at once).

    Not counted (P0-6): the failed messages of a ``key_mismatch`` system alert that has
    resolved. They could never be sent (the channels' credentials couldn't be
    decrypted); its RESOLVED message stands in for them. They stay ``failed``."""
    now = now or utcnow()
    resolved_key_mismatch = select(AlertEvent.id).where(
        AlertEvent.system_kind == SystemKind.KEY_MISMATCH.value,
        AlertEvent.state == AlertState.RESOLVED,
    )
    failed = (
        (N.status == Status.FAILED)
        & (N.finished_at >= now - FAILED_BANNER_WINDOW)
        & ~(N.event_id.is_not(None) & N.event_id.in_(resolved_key_mismatch))
    )
    return int(
        db.scalar(
            select(func.count()).where(
                (N.status.in_(UNFINISHED) & (N.created_at < now - STUCK_AFTER)) | failed
            )
        )
        or 0
    )


# --- the dispatcher ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Claim:
    token: str
    channel_id: int
    head_id: int
    row_ids: tuple[int, ...]
    text: str
    config: BaseModel | None
    # Set when the channel can't be used at all (permanent): its error kind.
    unusable: str | None


@dataclass(frozen=True)
class _Outcome:
    kind: str | None = None  # None: delivered
    permanent: bool = False
    retry_after: float | None = None


class DeliveryDispatcher:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        secret_box: SecretBox,
        client_factory: ClientFactory = default_client_factory,
        clock: Callable[[], datetime] = utcnow,
        poll_seconds: float = POLL_SECONDS,
        max_in_flight: int = MAX_IN_FLIGHT,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._sessions = session_factory
        self._box = secret_box
        self._client_factory = client_factory
        self._clock = clock
        self._poll = poll_seconds
        self._max_in_flight = max_in_flight
        self._send_timeout = send_timeout
        self._in_flight: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Rows that ended ``failed`` since this process started (a Prometheus counter).
        self.failed_total = 0

    # lifecycle

    def start(self) -> None:
        if self._task is None:
            try:
                self.recover()
            except Exception as exc:
                log.warning("alert delivery recovery failed (%s)", type(exc).__name__)
            self._task = asyncio.create_task(self._run(), name="vantage-alert-delivery")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._in_flight) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._in_flight.clear()

    def wake(self) -> None:
        """Look for work now instead of at the next poll."""
        self._wake.set()

    def recover(self) -> int:
        """Every row left ``sending`` (a crash or a stop mid-send) goes back to pending.
        Its attempt was counted at claim time. Returns how many."""
        with self._sessions() as db:
            n = self._release(db, N.status == Status.SENDING)
            db.commit()
        if n:
            log.info("alert delivery: %d unfinished send(s) queued again after a restart", n)
        return n

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                self._fill()
            except Exception as exc:  # never let one bad pass stop the loop
                log.warning("alert delivery pass failed (%s)", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll)

    def _fill(self) -> None:
        free = self._max_in_flight - len(self._in_flight)
        if free <= 0:
            return
        for claim in self._claim(free):
            task = asyncio.create_task(self._send(claim), name="vantage-alert-send")
            self._in_flight.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._in_flight.discard(task)
        self._wake.set()

    async def run_once(self) -> int:
        """Claim what is due and send it, waiting for the sends (tests, drills).
        Returns the number of messages attempted."""
        claims = self._claim(self._max_in_flight - len(self._in_flight))
        await asyncio.gather(*(self._send(c) for c in claims))
        return len(claims)

    # claiming

    def _release(self, db: Session, condition: object) -> int:
        result = db.execute(
            update(N)
            .where(condition)  # type: ignore[arg-type]
            .values(
                status=Status.PENDING,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=self._clock(),
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    def _claim(self, limit: int) -> list[_Claim]:
        if limit <= 0:
            return []
        now = self._clock()
        with self._sessions() as db:
            # Idle (nothing unfinished): a read only, so SQLite isn't write-locked each pass.
            if db.scalar(select(N.id).where(N.status.in_(UNFINISHED)).limit(1)) is None:
                return []
            # Safety nets: the channel went away; a claim lost its sender.
            orphaned = (N.channel_id.is_(None)) & (N.status == Status.PENDING)
            if db.scalar(select(N.id).where(orphaned).limit(1)) is not None:
                db.execute(
                    update(N)
                    .where(orphaned)
                    .values(status=Status.CANCELLED, finished_at=now)
                    .execution_options(synchronize_session=False)
                )
            stale = (N.status == Status.SENDING) & (N.claimed_at < now - STALE_CLAIM)
            if db.scalar(select(N.id).where(stale).limit(1)) is not None:
                self._release(db, stale)
            heads = (
                select(func.min(N.id))
                .where(N.status.in_(UNFINISHED), N.channel_id.is_not(None))
                .group_by(N.channel_id)
            )
            due = list(
                db.execute(
                    select(N.id, N.channel_id)
                    .where(
                        N.id.in_(heads),
                        N.status == Status.PENDING,
                        N.next_attempt_at <= now,
                    )
                    .order_by(N.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=N)
                )
            )
            claimed: list[tuple[str, int, int]] = []
            for head_id, channel_id in due:
                if channel_id is None:  # excluded by the query
                    continue
                token = uuid.uuid4().hex
                taken = db.execute(
                    update(N)
                    .where(N.id == head_id, N.status == Status.PENDING)
                    .values(
                        status=Status.SENDING,
                        claim_token=token,
                        claimed_at=now,
                        attempts=N.attempts + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if not taken.rowcount:  # type: ignore[attr-defined]
                    continue
                db.execute(  # the rows this message carries
                    update(N)
                    .where(N.channel_id == channel_id, N.status == Status.PENDING, N.id > head_id)
                    .values(
                        status=Status.SENDING,
                        claim_token=token,
                        claimed_at=now,
                        attempts=N.attempts + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                claimed.append((token, head_id, channel_id))
            db.commit()
            return [self._load_claim(db, *c) for c in claimed]

    def _load_claim(self, db: Session, token: str, head_id: int, channel_id: int) -> _Claim:
        rows = list(
            db.execute(select(N.id, N.text, N.kind).where(N.claim_token == token).order_by(N.id))
        )
        summaries = [r.id for r in rows if r.kind == Kind.SUMMARY]
        carried: dict[int, int] = {}
        if summaries:
            carried = dict(
                db.execute(  # type: ignore[arg-type]
                    select(N.folded_into, func.count())
                    .where(N.folded_into.in_(summaries))
                    .group_by(N.folded_into)
                ).all()
            )
        config: BaseModel | None = None
        unusable: str | None = None
        channel = db.get(Channel, channel_id)
        if channel is None:
            unusable = "channel_deleted"
        else:
            try:
                config = load_config(self._box, channel)
            except DecryptionError:
                unusable = "credentials_unreadable"
            except (ValueError, KeyError):
                unusable = "config_invalid"
        return _Claim(
            token=token,
            channel_id=channel_id,
            head_id=head_id,
            row_ids=tuple(r.id for r in rows),
            text=batch_text(
                [r.text for r in rows],
                [carried.get(r.id, 1) if r.kind == Kind.SUMMARY else 1 for r in rows],
            ),
            config=config,
            unusable=unusable,
        )

    # sending

    async def _send(self, claim: _Claim) -> None:
        if claim.unusable is not None or claim.config is None:
            outcome = _Outcome(claim.unusable or "config_invalid", permanent=True)
        else:
            outcome = await self._attempt(claim.config, claim.text)
        try:
            self._finish(claim, outcome)
        except Exception as exc:  # the rows stay "sending" and are released later
            log.warning("alert delivery bookkeeping failed (%s)", type(exc).__name__)

    async def _attempt(self, config: BaseModel, text: str) -> _Outcome:
        try:
            async with self._client_factory() as client:
                await asyncio.wait_for(deliver(client, config, text), self._send_timeout)
        except SendError as exc:
            return _Outcome(exc.kind, exc.permanent, exc.retry_after)
        except TimeoutError:
            return _Outcome("timeout")
        except asyncio.CancelledError:
            raise
        except Exception:  # its text could hold the request URL: the kind only
            return _Outcome("internal")
        return _Outcome()

    def _finish(self, claim: _Claim, outcome: _Outcome) -> None:
        now = self._clock()
        mine = (N.claim_token == claim.token) & (N.status == Status.SENDING)
        with self._sessions() as db:
            attempts = db.scalar(select(N.attempts).where(N.id == claim.head_id)) or 0
            if outcome.kind is None:
                db.execute(
                    update(N)
                    .where(mine, N.id == claim.head_id)
                    .values(status=Status.DELIVERED, finished_at=now, error_kind=None)
                    .execution_options(synchronize_session=False)
                )
                db.execute(
                    update(N)
                    .where(mine, N.id != claim.head_id)
                    .values(status=Status.FOLDED, folded_into=claim.head_id, finished_at=now)
                    .execution_options(synchronize_session=False)
                )
                settle_overflow(db, claim.row_ids, now)
                db.commit()
                log.info(
                    "alert notification delivered: channel %s, rows %s, attempts %d",
                    claim.channel_id,
                    _ids(claim.row_ids),
                    attempts,
                )
                return

            log.warning(
                "alert notification to channel %s failed: %s", claim.channel_id, outcome.kind
            )
            failed = 0
            if outcome.permanent:
                failed = self._fail(db, mine, outcome.kind, now)
            else:
                if outcome.retry_after is not None:  # at least 1 s: no tight resend loop
                    wait = min(max(outcome.retry_after, 1.0), MAX_WAIT_SECONDS)
                else:
                    wait = backoff_seconds(attempts)
                failed = self._fail(db, mine & (N.created_at <= now - MAX_AGE), "expired", now)
                db.execute(
                    update(N)
                    .where(mine)
                    .values(
                        status=Status.PENDING,
                        claim_token=None,
                        claimed_at=None,
                        error_kind=outcome.kind,
                        next_attempt_at=now + timedelta(seconds=wait),
                    )
                    .execution_options(synchronize_session=False)
                )
            failed += settle_overflow(db, claim.row_ids, now)
            db.commit()
            self.failed_total += failed

    @staticmethod
    def _fail(db: Session, condition: object, kind: str | None, now: datetime) -> int:
        result = db.execute(
            update(N)
            .where(condition)  # type: ignore[arg-type]
            .values(status=Status.FAILED, error_kind=kind, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    # metrics

    def pending_count(self) -> int:
        with self._sessions() as db:
            return int(db.scalar(select(func.count()).where(N.status.in_(UNFINISHED))) or 0)


def _ids(ids: Sequence[int]) -> str:
    if len(ids) <= 5:
        return ",".join(str(i) for i in ids)
    return f"{ids[0]}..{ids[-1]} ({len(ids)})"


class DeliveryCollector(Collector):
    """``vantage_alert_notifications_pending`` and ``..._failed_total`` on ``/metrics``."""

    def __init__(self, dispatcher: DeliveryDispatcher) -> None:
        self._dispatcher = dispatcher

    def collect(self) -> Iterable[Metric]:
        pending = GaugeMetricFamily(
            "vantage_alert_notifications_pending",
            "Alert notifications queued or being sent.",
        )
        try:
            pending.add_metric([], float(self._dispatcher.pending_count()))
        except Exception as exc:  # a scrape must not fail because the database hiccuped
            log.warning("counting queued alert notifications failed (%s)", type(exc).__name__)
        failed = CounterMetricFamily(
            "vantage_alert_notifications_failed",
            "Alert notifications that ended failed since the backend started.",
        )
        failed.add_metric([], float(self._dispatcher.failed_total))
        return [pending, failed]
