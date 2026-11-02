"""The alert state machine (P0-3b): pure functions, no I/O.

The evaluator gathers one :class:`Observation` per alert per tick (the probe, the rule's
result, the witness, the source's freshness and the clock) and calls :func:`step_pending`
for a series that has no open event, or :func:`step_open` for one that has. They return the
new state and the notifications to queue; the evaluator stores both in one transaction.

States: NORMAL (nothing kept), PENDING (memory only), FIRING and NO DATA (stored as the
event's ``state``), RECOVERING (one clean check seen; memory only, the stored state is
unchanged until the second one resolves the event).

The core rule: an alert resolves only after **two clean checks** on two different polls,
where a fresh value was seen and the condition was false. A missing series, a stale source
or a failed query never counts as recovery; they lead to NO DATA, which keeps the alert
open.

Shared causes (P0-4): when Prometheus has been unreachable for 2 minutes, or isn't
collecting Vantage's metrics (polled devices only), the evaluator passes the cause as
``shared_cause``: the alert goes into NO DATA with that reason whatever else it saw, its
NO DATA message is folded into the one system alert (``no_data_wait`` zero), no 24-hour
reminder is sent for it, and it returns to FIRING without a "data back" message (the
system alert's RESOLVED message counts it).
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from app.alerts.promql import RecoveryMode

# How long an alert stays in NO DATA before its message is queued.
NO_DATA_MESSAGE_AFTER = timedelta(minutes=5)
# One reminder after this long in NO DATA, no more.
NO_DATA_REMINDER_AFTER = timedelta(hours=24)
# The rule's query failing this many ticks in a row puts its alerts into NO DATA.
QUERY_FAILED_TICKS = 3
# A pending timer without data this long is dropped.
PENDING_RESET_AFTER = timedelta(minutes=10)
# A series with no source (an aggregate) missing from both results this long is
# ``metric_missing`` (the scraped freshness window).
AGGREGATE_MISSING_AFTER = timedelta(seconds=90)
# A series with no source can't prove two different polls, so its two clean checks must be
# this far apart, and none counts this soon after the evaluator started (devices that
# feed the aggregate may not have polled yet after a restart).
AGGREGATE_CLEAN_GAP = timedelta(seconds=60)
AGGREGATE_WARMUP = timedelta(seconds=150)


class NoDataReason(StrEnum):
    SOURCE_STALE = "source_stale"
    SOURCE_DOWN = "source_down"
    METRIC_MISSING = "metric_missing"
    QUERY_FAILED = "query_failed"
    # Shared causes (P0-4): one system alert speaks for every alert they affect.
    PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
    METRICS_NOT_COLLECTED = "metrics_not_collected"


SHARED_CAUSES = frozenset({NoDataReason.PROMETHEUS_UNREACHABLE, NoDataReason.METRICS_NOT_COLLECTED})


class Notice(StrEnum):
    """A message to queue (through the outbox)."""

    FIRING = "firing"
    RESOLVED = "resolved"
    NO_DATA = "no_data"
    DATA_BACK = "data_back"
    REMINDER = "reminder"


SourceKind = Literal["none", "fresh", "stale", "down", "unknown"]
Witness = Literal["false", "true", "absent", "failed", "none"]


@dataclass(frozen=True)
class Source:
    """The freshness of the device behind a series.

    ``none``: the series names no device (an aggregate): no source check. ``unknown``: the
    freshness queries failed this tick; nothing counts either way. ``ts`` is the source's
    last observation (Unix seconds): the last successful poll, or the last ``up == 1``
    scrape.
    """

    kind: SourceKind
    ts: float | None = None


NO_SOURCE = Source("none")


@dataclass(frozen=True)
class Observation:
    now: datetime
    # vector(1) answered: when it didn't, nothing changes and nothing counts.
    probe_ok: bool = True
    # The rule's query succeeded.
    query_ok: bool = True
    # The series is in the rule's (scoped) result.
    in_result: bool = False
    # Value rules: the witness verdict for this series ("none" for empty_result rules).
    witness: Witness = "none"
    source: Source = NO_SOURCE
    mode: RecoveryMode = "value"
    # When the evaluator started (None: long ago).
    started_at: datetime | None = None
    # A shared cause in force for this alert (P0-4): NO DATA with this reason, whatever
    # the rest of the observation says.
    shared_cause: NoDataReason | None = None
    # How long the NO DATA message waits; None holds it (the device is in a failure
    # streak and about to be marked down), zero when a shared cause's system alert
    # carries it.
    no_data_wait: timedelta | None = NO_DATA_MESSAGE_AFTER


@dataclass(frozen=True)
class Pending:
    started_at: datetime
    # Seconds the series has been in the result, gaps without data not counted.
    elapsed: float
    # The last tick the series was in the result.
    last_at: datetime
    paused: bool = False


@dataclass(frozen=True)
class OpenAlert:
    """An alert with an open event. The first four fields are stored on the event; the
    rest live in memory and start over on a restart."""

    state: Literal["firing", "no_data"]
    state_since: datetime
    no_data_reason: NoDataReason | None = None
    reminder_sent_at: datetime | None = None
    # Clean checks in a row (1 = RECOVERING).
    clean_checks: int = 0
    # The source observation the last clean check, or the last sighting, used.
    ref_ts: float | None = None
    # The last tick a value was seen (in the result or in the witness).
    last_value_at: datetime | None = None
    query_failures: int = 0
    # The NO DATA message for this episode was queued.
    no_data_sent: bool = False
    # When the first clean check of the current RECOVERING run happened.
    clean_at: datetime | None = None

    @property
    def recovering(self) -> bool:
        return self.clean_checks > 0


def no_data_message_due(
    alert: OpenAlert, now: datetime, wait: timedelta | None = NO_DATA_MESSAGE_AFTER
) -> bool:
    """Whether the NO DATA message is queued now: after ``wait`` in NO DATA (None: held).
    The evaluator decides where it goes (sent, or folded into a shared message)."""
    return (
        wait is not None
        and alert.state == "no_data"
        and not alert.no_data_sent
        and not alert.recovering  # a clean check was just seen: wait for the next tick
        and now - alert.state_since >= wait
    )


def _value_seen(obs: Observation) -> bool:
    return obs.in_result or obs.witness in ("false", "true")


def step_pending(
    pending: Pending | None, obs: Observation, for_seconds: int
) -> tuple[Pending | None, bool]:
    """A series without an open event. Returns (new pending state or None, fire now)."""
    now = obs.now
    has_data = obs.probe_ok and obs.query_ok and obs.shared_cause is None
    if obs.in_result and has_data:
        if pending is None:
            pending = Pending(started_at=now, elapsed=0.0, last_at=now)
        elif pending.paused:  # the gap without data doesn't count
            pending = replace(pending, last_at=max(now, pending.last_at), paused=False)
        else:
            elapsed = pending.elapsed + max(0.0, (now - pending.last_at).total_seconds())
            # A clock that went back never moves last_at back (nor counts twice).
            pending = replace(pending, elapsed=elapsed, last_at=max(now, pending.last_at))
        return pending, pending.elapsed >= for_seconds
    if pending is None:
        return None, False
    if has_data:
        if obs.mode == "empty_result" or obs.witness == "false":
            return None, False  # a value was seen and the condition is false: NORMAL
    # No fresh value (absent from both, a failed query or probe): paused, not reset.
    if now - pending.last_at >= PENDING_RESET_AFTER:
        return None, False
    return replace(pending, paused=True), False


def _aggregate_may_count(alert: OpenAlert, obs: Observation) -> bool:
    if obs.started_at is not None and obs.now - obs.started_at < AGGREGATE_WARMUP:
        return False
    if alert.clean_checks >= 1 and alert.clean_at is not None:
        return obs.now - alert.clean_at >= AGGREGATE_CLEAN_GAP
    return True


def _source_advanced(alert: OpenAlert, source: Source) -> bool:
    if source.kind == "none":
        return True
    if source.kind != "fresh" or source.ts is None:
        return False
    return alert.ref_ts is None or source.ts > alert.ref_ts


def _enter_no_data(alert: OpenAlert, reason: NoDataReason, now: datetime) -> OpenAlert:
    if alert.state == "no_data":
        return replace(alert, no_data_reason=reason)
    return replace(
        alert,
        state="no_data",
        no_data_reason=reason,
        state_since=now,
        reminder_sent_at=None,
        no_data_sent=False,
    )


def _no_data_reason(alert: OpenAlert, obs: Observation) -> NoDataReason | None:
    """Why there is no fresh value this tick, if there is a reason to say so."""
    source = obs.source
    if source.kind == "down":
        return NoDataReason.SOURCE_DOWN
    if source.kind == "stale":
        return NoDataReason.SOURCE_STALE
    if obs.mode == "empty_result" or obs.witness != "absent":
        return None
    if source.kind == "fresh":
        # Missing from a newer poll than the one it was last seen in.
        if alert.ref_ts is None or (source.ts is not None and source.ts > alert.ref_ts):
            return NoDataReason.METRIC_MISSING
        return None
    if source.kind == "none":
        seen = alert.last_value_at
        if seen is None or obs.now - seen >= AGGREGATE_MISSING_AFTER:
            return NoDataReason.METRIC_MISSING
    return None


def step_open(alert: OpenAlert, obs: Observation) -> tuple[OpenAlert | None, list[Notice]]:
    """An alert with an open event. Returns (new state, or None once RESOLVED; notices)."""
    now = obs.now
    notices: list[Notice] = []
    if obs.shared_cause is not None:
        # Prometheus down for 2 min, or not collecting: nothing counts, NO DATA.
        alert = replace(alert, clean_checks=0, query_failures=0)
        alert = _enter_no_data(alert, obs.shared_cause, now)
    elif not obs.probe_ok:  # Prometheus itself is unreachable: no change, nothing counts
        return alert, []
    elif not obs.query_ok or obs.witness == "failed":
        failures = alert.query_failures + 1
        alert = replace(alert, query_failures=failures, clean_checks=0)
        if failures >= QUERY_FAILED_TICKS:
            alert = _enter_no_data(alert, NoDataReason.QUERY_FAILED, now)
    elif obs.in_result or obs.witness == "true":
        # The condition is true: FIRING (from RECOVERING without a new message).
        ref = obs.source.ts if obs.source.kind == "fresh" else alert.ref_ts
        shared = alert.no_data_reason in SHARED_CAUSES  # counted by the system alert
        if alert.state == "no_data" and alert.no_data_sent and not shared:
            notices.append(Notice.DATA_BACK)
        if alert.state == "no_data":
            alert = replace(
                alert,
                state="firing",
                state_since=now,
                no_data_reason=None,
                reminder_sent_at=None,
                no_data_sent=False,
            )
        alert = replace(alert, clean_checks=0, ref_ts=ref, last_value_at=now, query_failures=0)
    else:
        alert = replace(alert, query_failures=0)
        if _value_seen(obs):
            alert = replace(alert, last_value_at=now)
        witness_clean = obs.mode == "empty_result" or obs.witness == "false"
        aggregate_wait = obs.source.kind == "none" and not _aggregate_may_count(alert, obs)
        if witness_clean and not aggregate_wait and _source_advanced(alert, obs.source):
            if alert.clean_checks >= 1:
                return None, [Notice.RESOLVED]
            alert = replace(alert, clean_checks=1, ref_ts=obs.source.ts, clean_at=now)
        elif witness_clean and obs.source.kind in ("fresh", "unknown", "none"):
            # The same poll seen again, freshness unknown, or an aggregate too soon after
            # the last clean check (or the start): neither clean nor a reset.
            pass
        else:
            reason = _no_data_reason(alert, obs)
            alert = replace(alert, clean_checks=0)
            if reason is not None:
                alert = _enter_no_data(alert, reason, now)

    if no_data_message_due(alert, now, obs.no_data_wait):
        notices.append(Notice.NO_DATA)
        alert = replace(alert, no_data_sent=True)
    if (
        alert.state == "no_data"
        and alert.no_data_reason not in SHARED_CAUSES
        and alert.no_data_sent
        and alert.reminder_sent_at is None
        and not alert.recovering
        and now - alert.state_since >= NO_DATA_REMINDER_AFTER
    ):
        notices.append(Notice.REMINDER)
        alert = replace(alert, reminder_sent_at=now)
    return alert, notices
