import math
import uuid
from datetime import UTC, datetime

from prometheus_client import generate_latest

from app.collectors.store import (
    MAX_SAMPLES_PER_INSTANCE,
    MetricsStore,
    make_registry,
    standard_labels,
)
from app.integrations.base import Sample

AT = datetime(2026, 1, 1, tzinfo=UTC)
ID_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
ID_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def labels(instance_id: uuid.UUID, name: str = "nas-1") -> dict[str, str]:
    return standard_labels(instance_id=instance_id, name=name, type_id="fake_device", site="lab")


def render(store: MetricsStore) -> str:
    return generate_latest(make_registry(store)).decode()


def gauge(name: str, value: float = 1.0, **extra: str) -> Sample:
    return Sample(name=name, value=value, type="gauge", labels=extra)


def test_standard_labels() -> None:
    assert standard_labels(instance_id=ID_A, name="nas-1", type_id="fake_device", site=None) == {
        "vantage_instance_id": str(ID_A),
        "vantage_instance": "nas-1",
        "vantage_integration": "fake_device",
        "vantage_site": "",
    }


def test_pending_instance_is_not_exposed() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    assert store.status(ID_A) is not None
    assert store.status(ID_A).state == "pending"  # type: ignore[union-attr]
    assert "vantage_instance_up{" not in render(store)


def test_success_adds_standard_labels_and_up() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    dropped = store.record_success(
        ID_A,
        "fake_device",
        [gauge("vantage_fake_device_temp_celsius", 41.5, sensor="cpu")],
        at=AT,
        duration_seconds=0.25,
    )
    assert dropped == 0
    text = render(store)
    std = (
        f'vantage_instance="nas-1",vantage_instance_id="{ID_A}",'
        'vantage_integration="fake_device",vantage_site="lab"'
    )
    assert f"vantage_instance_up{{{std}}} 1.0" in text
    assert f'vantage_fake_device_temp_celsius{{sensor="cpu",{std}}} 41.5' in text
    assert f"vantage_poll_duration_seconds{{{std}}} 0.25" in text
    line = next(x for x in text.splitlines() if x.startswith("vantage_poll_last_success"))
    assert line.startswith(f"vantage_poll_last_success_timestamp_seconds{{{std}}} ")
    assert float(line.rsplit(" ", 1)[1]) == AT.timestamp()
    assert "# TYPE vantage_fake_device_temp_celsius gauge" in text


def test_failure_marks_down_and_drops_old_samples() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    store.record_success(
        ID_A, "fake_device", [gauge("vantage_fake_device_x")], at=AT, duration_seconds=0.1
    )
    store.record_failure(ID_A, "poll timed out", at=AT, duration_seconds=5.0)
    status = store.status(ID_A)
    assert status is not None
    assert status.state == "down"
    assert status.error == "poll timed out"
    assert status.last_success_at == AT  # kept from the last success
    text = render(store)
    assert 'vantage_instance_up{vantage_instance="nas-1"' in text
    assert "} 0.0" in text
    assert "vantage_fake_device_x{" not in text


def test_counters_are_exposed_as_total() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    store.record_success(
        ID_A,
        "fake_device",
        [
            Sample(name="vantage_fake_device_rx_bytes", value=10, type="counter"),
            Sample(name="vantage_fake_device_tx_bytes_total", value=20, type="counter"),
        ],
        at=AT,
        duration_seconds=0.1,
    )
    text = render(store)
    assert "# TYPE vantage_fake_device_rx_bytes_total counter" in text
    assert "vantage_fake_device_rx_bytes_total{" in text
    assert "vantage_fake_device_tx_bytes_total{" in text


def test_invalid_samples_are_dropped() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    bad = [
        gauge("vantage_other_type_x"),  # another integration's prefix
        gauge("not_vantage"),
        gauge("vantage_fake_device_bad-name"),
        gauge("vantage_fake_device_x", vantage_instance="spoofed"),  # overrides a standard label
        gauge("vantage_fake_device_x", vantage_site="spoofed"),
        gauge("vantage_fake_device_x", __name__="spoofed"),
        gauge("vantage_fake_device_x", **{"bad-label": "v"}),
        gauge("vantage_fake_device_x", long="v" * 5000),
        Sample(name="vantage_fake_device_c", value=-1, type="counter"),
        Sample(name="vantage_fake_device_c", value=math.inf, type="counter"),
    ]
    good = gauge("vantage_fake_device_x", 2.0)
    dropped = store.record_success(
        ID_A, "fake_device", [*bad, good, good], at=AT, duration_seconds=0.1
    )
    assert dropped == len(bad) + 1  # plus the duplicate series
    text = render(store)
    assert "spoofed" not in text
    assert "vantage_other_type_x" not in text
    assert "vantage_fake_device_c" not in text
    assert text.count("vantage_fake_device_x{") == 1
    assert 'vantage_poll_dropped_samples{vantage_instance="nas-1"' in text


def test_own_metric_names_cannot_be_used() -> None:
    store = MetricsStore()
    std = standard_labels(instance_id=ID_A, name="a", type_id="instance", site=None)
    store.track(ID_A, std)
    # An integration whose type_id is "instance" would collide with vantage_instance_up.
    dropped = store.record_success(
        ID_A, "instance", [gauge("vantage_instance_up", 7.0)], at=AT, duration_seconds=0.1
    )
    assert dropped == 1
    assert "7.0" not in render(store)


def test_sample_cap() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    samples = [
        gauge("vantage_fake_device_x", i=str(i)) for i in range(MAX_SAMPLES_PER_INSTANCE + 5)
    ]
    assert store.record_success(ID_A, "fake_device", samples, at=AT, duration_seconds=0.1) == 5


def test_conflicting_families_dont_break_the_exposition() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A, "a"))
    store.track(ID_B, labels(ID_B, "b"))
    store.record_success(
        ID_A, "fake_device", [gauge("vantage_fake_device_x_total")], at=AT, duration_seconds=0.1
    )
    store.record_success(
        ID_B,
        "fake_device",
        [
            Sample(name="vantage_fake_device_x", value=3, type="counter"),  # exposes x_total
            gauge("vantage_fake_device_x_total", 5.0),
        ],
        at=AT,
        duration_seconds=0.1,
    )
    text = render(store)
    assert text.count("# TYPE vantage_fake_device_x_total gauge") == 1
    assert " counter" not in text
    assert text.count("vantage_fake_device_x_total{") == 2  # one per instance, both gauges


def test_untracked_instance_ignored_and_remove() -> None:
    store = MetricsStore()
    assert store.record_success(ID_A, "fake_device", [], at=AT, duration_seconds=0.1) == 0
    store.record_failure(ID_A, "x", at=AT, duration_seconds=0.1)
    assert store.status(ID_A) is None
    store.track(ID_A, labels(ID_A))
    store.remove(ID_A)
    assert store.tracked() == frozenset()


def test_relabel_clears_samples() -> None:
    store = MetricsStore()
    store.track(ID_A, labels(ID_A))
    store.record_success(
        ID_A, "fake_device", [gauge("vantage_fake_device_x")], at=AT, duration_seconds=0.1
    )
    store.track(ID_A, labels(ID_A, "renamed"))
    text = render(store)
    assert "vantage_fake_device_x{" not in text
    assert 'vantage_instance="renamed"' in text
