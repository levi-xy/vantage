"""The alert state machine as a pure function (P0-3b): one case per transition."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts.state import (
    NO_SOURCE,
    NoDataReason,
    Notice,
    Observation,
    OpenAlert,
    Pending,
    Source,
    step_open,
    step_pending,
)

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
S = 1_000.0  # a source observation (Unix seconds)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def fresh(ts: float = S) -> Source:
    return Source("fresh", ts)


FIRING = OpenAlert(state="firing", state_since=T0, ref_ts=S, last_value_at=T0)
RECOVERING = replace(FIRING, clean_checks=1, ref_ts=S + 60)
NO_DATA = replace(FIRING, state="no_data", no_data_reason=NoDataReason.SOURCE_STALE, state_since=T0)


# --- PENDING ----------------------------------------------------------------------------

PENDING = Pending(started_at=T0, elapsed=30.0, last_at=T0)


@pytest.mark.parametrize(
    ("name", "pending", "obs", "for_seconds", "expected", "fire"),
    [
        ("NORMAL -> PENDING", None, Observation(now=T0, in_result=True), 60, (0.0, False), False),
        ("for 0 fires at once", None, Observation(now=T0, in_result=True), 0, (0.0, False), True),
        (
            "PENDING counts",
            PENDING,
            Observation(now=at(30), in_result=True),
            60,
            (60.0, False),
            True,
        ),
        (
            "PENDING -> NORMAL: value seen, condition false",
            PENDING,
            Observation(now=at(30), witness="false"),
            60,
            None,
            False,
        ),
        (
            "paused: no fresh value",
            PENDING,
            Observation(now=at(30), witness="absent"),
            60,
            (30.0, True),
            False,
        ),
        (
            "paused: query failed",
            PENDING,
            Observation(now=at(30), query_ok=False),
            60,
            (30.0, True),
            False,
        ),
        (
            "paused: probe failed",
            PENDING,
            Observation(now=at(30), probe_ok=False, in_result=True),
            60,
            (30.0, True),
            False,
        ),
        (
            "reset after 10 min without data",
            replace(PENDING, paused=True),
            Observation(now=at(600), witness="absent"),
            60,
            None,
            False,
        ),
        (
            "resumes without counting the gap",
            replace(PENDING, paused=True),
            Observation(now=at(300), in_result=True),
            60,
            (30.0, False),
            False,
        ),
        (
            "empty_result rule: absence is NORMAL",
            PENDING,
            Observation(now=at(30), mode="empty_result"),
            60,
            None,
            False,
        ),
    ],
)
def test_pending(
    name: str,
    pending: Pending | None,
    obs: Observation,
    for_seconds: int,
    expected: tuple[float, bool] | None,
    fire: bool,
) -> None:
    new, fired = step_pending(pending, obs, for_seconds)
    assert fired is fire, name
    if expected is None:
        assert new is None, name
    else:
        assert new is not None and (new.elapsed, new.paused) == expected, name


def test_flapping_never_fires() -> None:
    """30 s on, 30 s off, for 180."""
    pending = None
    for i in range(20):
        on = i % 2 == 0
        obs = Observation(now=at(30 * i), in_result=on, witness="true" if on else "false")
        pending, fired = step_pending(pending, obs, 180)
        assert not fired


# --- open alerts ------------------------------------------------------------------------


def run(alert: OpenAlert, obs: Observation) -> tuple[OpenAlert | None, list[Notice]]:
    return step_open(alert, obs)


@pytest.mark.parametrize(
    ("name", "alert", "obs", "state", "clean", "notices"),
    [
        (
            "FIRING -> RECOVERING: first clean check",
            FIRING,
            Observation(now=at(60), witness="false", source=fresh(S + 60)),
            "firing",
            1,
            [],
        ),
        (
            "RECOVERING -> RESOLVED: second clean check from a newer poll",
            RECOVERING,
            Observation(now=at(120), witness="false", source=fresh(S + 120)),
            None,
            0,
            [Notice.RESOLVED],
        ),
        (
            "RECOVERING: the same poll seen again counts nothing",
            RECOVERING,
            Observation(now=at(90), witness="false", source=fresh(S + 60)),
            "firing",
            1,
            [],
        ),
        (
            "RECOVERING -> FIRING: condition true again, no message",
            RECOVERING,
            Observation(now=at(90), in_result=True, witness="true", source=fresh(S + 90)),
            "firing",
            0,
            [],
        ),
        (
            "RECOVERING -> FIRING: neither clean nor true resets the count",
            RECOVERING,
            Observation(now=at(90), witness="absent", source=fresh(S + 60)),
            "firing",
            0,
            [],
        ),
        (
            "a series without a source: conditions 1-4 suffice",
            replace(RECOVERING, ref_ts=None),
            Observation(now=at(90), witness="false", source=NO_SOURCE),
            None,
            0,
            [Notice.RESOLVED],
        ),
        (
            "NO DATA -> RECOVERING: a clean check, stored state stays",
            NO_DATA,
            Observation(now=at(60), witness="false", source=fresh(S + 60)),
            "no_data",
            1,
            [],
        ),
        (
            "NO DATA -> FIRING quietly when no NO DATA message was sent",
            NO_DATA,
            Observation(now=at(60), in_result=True, source=fresh(S + 60)),
            "firing",
            0,
            [],
        ),
        (
            "NO DATA -> FIRING: data back, still firing",
            replace(NO_DATA, no_data_sent=True),
            Observation(now=at(600), in_result=True, source=fresh(S + 600)),
            "firing",
            0,
            [Notice.DATA_BACK],
        ),
        (
            "an empty_result rule: absence with a fresh, advanced source is clean",
            FIRING,
            Observation(now=at(60), mode="empty_result", source=fresh(S + 60)),
            "firing",
            1,
            [],
        ),
        (
            "an empty_result rule: absence with the same poll counts nothing",
            FIRING,
            Observation(now=at(60), mode="empty_result", source=fresh(S)),
            "firing",
            0,
            [],
        ),
        (
            "probe failed: nothing changes",
            RECOVERING,
            Observation(now=at(600), probe_ok=False),
            "firing",
            1,
            [],
        ),
        (
            "freshness unknown: nothing counts either way",
            RECOVERING,
            Observation(now=at(90), witness="false", source=Source("unknown")),
            "firing",
            1,
            [],
        ),
    ],
)
def test_open(
    name: str,
    alert: OpenAlert,
    obs: Observation,
    state: str | None,
    clean: int,
    notices: list[Notice],
) -> None:
    new, sent = run(alert, obs)
    assert sent == notices, name
    if state is None:
        assert new is None, name
    else:
        assert new is not None and (new.state, new.clean_checks) == (state, clean), name


@pytest.mark.parametrize(
    ("name", "obs", "reason"),
    [
        (
            "stale source",
            Observation(now=at(200), witness="absent", source=Source("stale", S)),
            NoDataReason.SOURCE_STALE,
        ),
        (
            "instance down",
            Observation(now=at(200), witness="absent", source=Source("down", S)),
            NoDataReason.SOURCE_DOWN,
        ),
        (
            "metric missing from a newer poll",
            Observation(now=at(120), witness="absent", source=fresh(S + 120)),
            NoDataReason.METRIC_MISSING,
        ),
        (
            "aggregate missing for a freshness window",
            Observation(now=at(90), witness="absent", source=NO_SOURCE),
            NoDataReason.METRIC_MISSING,
        ),
    ],
)
def test_firing_to_no_data(name: str, obs: Observation, reason: NoDataReason) -> None:
    for alert in (FIRING, RECOVERING):
        new, sent = run(alert, obs)
        assert new is not None and new.state == "no_data", name
        assert (new.no_data_reason, new.state_since, new.clean_checks) == (reason, obs.now, 0)
        assert sent == [], name  # the message waits 5 minutes


def test_missing_from_the_same_poll_is_not_yet_no_data() -> None:
    new, _ = run(FIRING, Observation(now=at(30), witness="absent", source=fresh(S)))
    assert new is not None and new.state == "firing"


def test_query_failing_three_ticks_then_message_after_five_minutes() -> None:
    alert: OpenAlert | None = FIRING
    for i in (1, 2):
        alert, _ = run(alert, Observation(now=at(30 * i), query_ok=False))  # type: ignore[arg-type]
        assert alert is not None and alert.state == "firing"
    alert, sent = run(alert, Observation(now=at(90), query_ok=False))  # type: ignore[arg-type]
    assert alert is not None and alert.no_data_reason == NoDataReason.QUERY_FAILED and sent == []
    alert, sent = run(alert, Observation(now=at(90 + 299), query_ok=False))
    assert sent == []
    alert, sent = run(alert, Observation(now=at(90 + 300), query_ok=False))  # type: ignore[arg-type]
    assert sent == [Notice.NO_DATA]
    alert, sent = run(alert, Observation(now=at(90 + 330), query_ok=False))  # type: ignore[arg-type]
    assert sent == []  # once


def test_one_reminder_after_24_hours() -> None:
    alert: OpenAlert | None = replace(NO_DATA, no_data_sent=True)
    stale = Source("stale", S)
    sent_all = []
    for hours in (23, 24, 25, 48):
        alert, sent = run(alert, Observation(now=at(hours * 3600), witness="absent", source=stale))  # type: ignore[arg-type]
        sent_all += sent
    assert sent_all == [Notice.REMINDER]
    assert alert is not None and alert.reminder_sent_at == at(24 * 3600)


def test_reason_updates_while_in_no_data_without_restarting_the_clock() -> None:
    new, _ = run(NO_DATA, Observation(now=at(200), witness="absent", source=Source("down", S)))
    assert new is not None
    assert (new.no_data_reason, new.state_since) == (NoDataReason.SOURCE_DOWN, T0)


def test_recovering_from_no_data_falls_back_to_no_data() -> None:
    recovering = replace(NO_DATA, clean_checks=1, ref_ts=S + 60)
    new, _ = run(recovering, Observation(now=at(90), witness="absent", source=Source("stale", S)))
    assert new is not None and (new.state, new.clean_checks) == ("no_data", 0)
