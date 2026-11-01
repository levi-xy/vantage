"""Latest poll results and status per instance, exposed in Prometheus format (M2.4).

The poll scheduler records each poll here; ``GET /metrics`` renders the store through
a ``prometheus_client`` collector. Results live in memory only: Prometheus keeps the
history, and a restart simply polls again (``last_success_at`` is seeded from
Prometheus at startup).

A failed poll cycle drops the instance's samples at once, but the instance only goes
``down`` after ``down_after_failed_cycles`` failed cycles in a row; one good cycle brings
it back ``up``.

Every sample gets the four standard labels (AGENTS.md Contract 1). Samples an
integration returns are checked before they are stored; a bad one is dropped (and
counted), never allowed to break the exposition for every other instance.
"""

import math
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector, CollectorRegistry

from app.core.config import DEFAULT_DOWN_AFTER_FAILED_CYCLES
from app.integrations.base import Sample

INSTANCE_ID = "vantage_instance_id"
INSTANCE = "vantage_instance"
INTEGRATION = "vantage_integration"
SITE = "vantage_site"
STANDARD_LABELS = frozenset({INSTANCE_ID, INSTANCE, INTEGRATION, SITE})

# Vantage's own metrics. Polled samples may never use these names.
UP = "vantage_instance_up"
POLL_DURATION = "vantage_poll_duration_seconds"
LAST_SUCCESS = "vantage_poll_last_success_timestamp_seconds"
DROPPED = "vantage_poll_dropped_samples"
FAILED_CYCLES = "vantage_poll_consecutive_failed_cycles"
OWN_METRICS = frozenset({UP, POLL_DURATION, LAST_SUCCESS, DROPPED, FAILED_CYCLES})

# Limits on what one poll may store, so one misbehaving integration can't exhaust memory.
MAX_SAMPLES_PER_INSTANCE = 10_000
MAX_LABELS_PER_SAMPLE = 32
MAX_LABEL_VALUE_LENGTH = 1024

_METRIC_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

State = Literal["pending", "up", "down"]


def standard_labels(
    *, instance_id: uuid.UUID, name: str, type_id: str, site: str | None
) -> dict[str, str]:
    """The four labels the collector puts on every polled sample."""
    return {
        INSTANCE_ID: str(instance_id),
        INSTANCE: name,
        INTEGRATION: type_id,
        SITE: site or "",
    }


@dataclass(frozen=True)
class InstanceStatus:
    """An instance's poll status. ``error`` is a short, secret-free description."""

    state: State
    checked_at: datetime | None = None
    last_success_at: datetime | None = None
    duration_seconds: float | None = None
    error: str | None = None


@dataclass
class _Entry:
    labels: dict[str, str]
    status: InstanceStatus = field(default_factory=lambda: InstanceStatus(state="pending"))
    samples: list[Sample] = field(default_factory=list)
    dropped: int = 0
    failed_cycles: int = 0


def _family_names(name: str, kind: str) -> tuple[str, frozenset[str]]:
    """``(family name, every series name it exposes)``. Counters are exposed as ``*_total``."""
    if kind == "counter":
        base = name.removesuffix("_total")
        return base, frozenset({base, f"{base}_total"})
    return name, frozenset({name})


def check_sample(sample: Sample, type_id: str) -> bool:
    """Whether a polled sample may be exposed: a ``vantage_<type_id>_*`` name that isn't one
    of Vantage's own, valid labels without the standard ones, and for counters a finite,
    non-negative value."""
    name = sample.name
    if not _METRIC_NAME.fullmatch(name) or not name.startswith(f"vantage_{type_id}_"):
        return False
    _, series = _family_names(name, sample.type)
    if series & OWN_METRICS:
        return False
    if len(sample.labels) > MAX_LABELS_PER_SAMPLE:
        return False
    for key, value in sample.labels.items():
        if key in STANDARD_LABELS or key.startswith("__") or not _LABEL_NAME.fullmatch(key):
            return False
        if len(value) > MAX_LABEL_VALUE_LENGTH:
            return False
    if sample.type == "counter":
        return math.isfinite(sample.value) and sample.value >= 0
    return True


class MetricsStore:
    """In-memory poll results. Used from the event loop only."""

    def __init__(self, *, down_after_failed_cycles: int = DEFAULT_DOWN_AFTER_FAILED_CYCLES) -> None:
        if down_after_failed_cycles < 1:
            raise ValueError("down_after_failed_cycles must be at least 1")
        self._down_after = down_after_failed_cycles
        self._entries: dict[uuid.UUID, _Entry] = {}
        # last_success_at values read from Prometheus at startup, for instances not tracked
        # yet; applied (once) when the instance is tracked.
        self._seeded: dict[uuid.UUID, datetime] = {}

    # --- writes -------------------------------------------------------------------------

    def track(self, instance_id: uuid.UUID, labels: Mapping[str, str]) -> None:
        """Start tracking an instance (status ``pending``) or refresh its labels."""
        entry = self._entries.get(instance_id)
        if entry is None:
            entry = _Entry(labels=dict(labels))
            seeded = self._seeded.pop(instance_id, None)
            if seeded is not None:
                entry.status = InstanceStatus(state="pending", last_success_at=seeded)
            self._entries[instance_id] = entry
        elif entry.labels != labels:
            entry.labels = dict(labels)
            entry.samples = []

    def record_success(
        self,
        instance_id: uuid.UUID,
        type_id: str,
        samples: Iterable[Sample],
        *,
        at: datetime,
        duration_seconds: float,
        forbidden: Iterable[str] = (),
    ) -> int:
        """Store a poll's samples, replacing the previous ones. Returns how many were dropped.

        A sample with any ``forbidden`` value (the instance's decrypted secrets) inside a
        label value is dropped too: integrations must not do that, but if one does, the
        secret never reaches /metrics.
        """
        secrets = [value for value in forbidden if value]
        entry = self._entries.get(instance_id)
        if entry is None:
            return 0
        kept: list[Sample] = []
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        dropped = 0
        for sample in samples:
            key = (sample.name, tuple(sorted(sample.labels.items())))
            if (
                len(kept) >= MAX_SAMPLES_PER_INSTANCE
                or key in seen
                or not check_sample(sample, type_id)
                or any(secret in value for secret in secrets for value in sample.labels.values())
            ):
                dropped += 1
                continue
            seen.add(key)
            kept.append(sample)
        entry.samples = kept
        entry.dropped = dropped
        entry.failed_cycles = 0
        entry.status = InstanceStatus(
            state="up",
            checked_at=at,
            last_success_at=at,
            duration_seconds=duration_seconds,
        )
        return dropped

    def record_failure(
        self,
        instance_id: uuid.UUID,
        error: str,
        *,
        at: datetime,
        duration_seconds: float,
    ) -> None:
        """Record one failed poll cycle.

        The old samples are dropped at once rather than served stale. The instance goes
        ``down`` only at the ``down_after_failed_cycles``-th failed cycle in a row; until
        then it keeps its state (``up``, or ``pending`` if it never answered) and shows
        ``error``.
        """
        entry = self._entries.get(instance_id)
        if entry is None:
            return
        entry.samples = []
        entry.dropped = 0
        entry.failed_cycles += 1
        state: State = "down" if entry.failed_cycles >= self._down_after else entry.status.state
        entry.status = InstanceStatus(
            state=state,
            checked_at=at,
            last_success_at=entry.status.last_success_at,
            duration_seconds=duration_seconds,
            error=error,
        )

    def seed_last_success(self, seeded: Mapping[uuid.UUID, float]) -> int:
        """Fill ``last_success_at`` from Unix times read from Prometheus at startup.

        Only an empty ``last_success_at`` is filled (a poll that already succeeded is
        newer); the state is left alone. Instances not tracked yet get the value when they
        are. Returns how many values were taken.
        """
        taken = 0
        for instance_id, timestamp in seeded.items():
            if not math.isfinite(timestamp) or timestamp <= 0:
                continue
            try:
                at = datetime.fromtimestamp(timestamp, UTC)
            except (OverflowError, OSError, ValueError):
                continue
            entry = self._entries.get(instance_id)
            if entry is None:
                self._seeded[instance_id] = at
            elif entry.status.last_success_at is None:
                entry.status = replace(entry.status, last_success_at=at)
            else:
                continue
            taken += 1
        return taken

    def remove(self, instance_id: uuid.UUID) -> None:
        self._entries.pop(instance_id, None)

    # --- reads --------------------------------------------------------------------------

    def tracked(self) -> frozenset[uuid.UUID]:
        return frozenset(self._entries)

    def samples(self, instance_id: uuid.UUID) -> list[Sample]:
        """The samples of the instance's last good poll (empty if none)."""
        entry = self._entries.get(instance_id)
        return list(entry.samples) if entry else []

    def status(self, instance_id: uuid.UUID) -> InstanceStatus | None:
        entry = self._entries.get(instance_id)
        return entry.status if entry is not None else None

    def failed_cycles(self, instance_id: uuid.UUID) -> int:
        """Failed poll cycles in a row (0 after a good one)."""
        entry = self._entries.get(instance_id)
        return entry.failed_cycles if entry is not None else 0

    def statuses(self) -> dict[uuid.UUID, InstanceStatus]:
        return {key: entry.status for key, entry in self._entries.items()}

    def collect(self) -> Iterator[Metric]:
        """Metric families for the exposition, own metrics first.

        Instances are emitted in a stable order. If two families would expose the same
        series name (e.g. a gauge ``x_total`` and a counter ``x``), the later one is
        dropped: a duplicate name would make Prometheus reject the whole scrape.
        """
        entries = sorted(self._entries.items(), key=lambda item: str(item[0]))
        up = Metric(UP, "0 once the instance is down (N failed cycles in a row), else 1.", "gauge")
        duration = Metric(POLL_DURATION, "How long the instance's last poll took.", "gauge")
        last_ok = Metric(LAST_SUCCESS, "Unix time of the instance's last successful poll.", "gauge")
        dropped = Metric(DROPPED, "Samples dropped from the last poll as invalid.", "gauge")
        failed = Metric(FAILED_CYCLES, "Failed poll cycles in a row (0 after a good one).", "gauge")
        families: dict[str, Metric] = {}
        taken: dict[str, str] = {name: name for name in OWN_METRICS}  # series -> family

        for _, entry in entries:
            status = entry.status
            if status.checked_at is None:  # not polled yet
                continue
            labels = entry.labels
            # A pending instance whose first cycles failed has no up/down answer yet.
            if status.state != "pending":
                up.add_sample(UP, labels, 1.0 if status.state == "up" else 0.0)
            failed.add_sample(FAILED_CYCLES, labels, float(entry.failed_cycles))
            if status.duration_seconds is not None:
                duration.add_sample(POLL_DURATION, labels, status.duration_seconds)
            if status.last_success_at is not None:
                last_ok.add_sample(LAST_SUCCESS, labels, status.last_success_at.timestamp())
            dropped.add_sample(DROPPED, labels, float(entry.dropped))

            for sample in entry.samples:
                family_name, series = _family_names(sample.name, sample.type)
                family = families.get(family_name)
                if family is None:
                    if any(taken.get(s, family_name) != family_name for s in series):
                        continue
                    family = Metric(family_name, "Polled by Vantage.", sample.type)
                    families[family_name] = family
                    taken.update(dict.fromkeys(series, family_name))
                elif family.type != sample.type:
                    continue
                exposed = f"{family_name}_total" if sample.type == "counter" else family_name
                family.add_sample(exposed, {**sample.labels, **labels}, sample.value)

        yield up
        yield duration
        yield last_ok
        yield dropped
        yield failed
        yield from families.values()


class _StoreCollector(Collector):
    def __init__(self, store: MetricsStore) -> None:
        self._store = store

    def collect(self) -> Iterable[Metric]:
        return list(self._store.collect())


def make_registry(store: MetricsStore) -> CollectorRegistry:
    """A dedicated registry (not prometheus_client's global one) serving ``store``."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_StoreCollector(store))
    return registry
