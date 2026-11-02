"""The alert expression splitter (P0-3b)."""

import pytest

from app.alerts.promql import Condition, Part, is_instance_down, split
from app.alerts.templates import TEMPLATES


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_template_has_a_witness(template: object) -> None:
    result = split(template.expr)  # type: ignore[attr-defined]
    assert result.mode == "value"
    for part in result.parts:
        # The value query is the rule minus its comparison, and itself has none at the top.
        assert split(part.value_query).mode == "empty_result"


@pytest.mark.parametrize(
    ("expr", "parts"),
    [
        ("A > 85", [("A", ">", 85)]),
        ("85 < A", [("A", ">", 85)]),
        ("0.2 >= rate(x[5m])", [("rate(x[5m])", "<=", 0.2)]),
        (
            'up{vantage_integration="pfsense"} == 0',
            [('up{vantage_integration="pfsense"}', "==", 0)],
        ),
        ("A != -1", [("A", "!=", -1)]),
        ("x > 1e3", [("x", ">", 1000)]),
        (
            '100 * (1 - a{fs="/"} / (b{x="("} + 1)) > 85',
            [('100 * (1 - a{fs="/"} / (b{x="("} + 1))', ">", 85)],
        ),
        ("A > 0 or B > 0", [("A", ">", 0), ("B", ">", 0)]),
        ("vector_or_not > 1", [("vector_or_not", ">", 1)]),
        ('x{l="a > 1 or b"} < 5', [('x{l="a > 1 or b"}', "<", 5)]),
        ('x{l="esc \\" > 3"} > 2', [('x{l="esc \\" > 3"}', ">", 2)]),
        ("x offset 5m > 2", [("x offset 5m", ">", 2)]),
        ("A > 0x1F", [("A", ">", 31)]),
        ("A > 0.5", [("A", ">", 0.5)]),
        (
            'sum by (job) (rate(x{a!="b"}[5m])) > 3',
            [('sum by (job) (rate(x{a!="b"}[5m]))', ">", 3)],
        ),
    ],
)
def test_splits(expr: str, parts: list[tuple[str, str, float]]) -> None:
    result = split(expr)
    assert result.mode == "value"
    assert result.parts == tuple(Part(q, Condition(op, t)) for q, op, t in parts)


@pytest.mark.parametrize(
    "expr",
    [
        "max(up > 0)",  # comparison inside a function
        "absent(up == 1)",
        'up{job=~"a|b"}',  # matcher only
        '"a > 1"',  # a string
        "A > bool 5",
        "A > 0 and B > 0",
        "A > 0 unless B",
        "A > 0 or B",
        "A > on(x) B",
        "A > B",  # two series
        "1 > 2",  # two numbers
        "A > 1 < 5",
        "A > 0 or on(x) B > 0",
        "((A > 5))",  # nested: the comparison isn't at the top level
        "A >",
        "sum(x",  # unbalanced
        'x{a="unterminated}',
        "A > 010",  # Prometheus: octal 8
        "A > 0b1",
        "A > 0o7",
        "absent(A) == 1",
        "absent_over_time(A[5m]) > 0",
        "count(A > 85) > 2",  # comparison inside a group
        "sum(A or B) > 1",
        "max_over_time((A > 1)[5m:]) > 0",
    ],
)
def test_unsure_means_empty_result(expr: str) -> None:
    assert split(expr).mode == "empty_result"


def test_condition() -> None:
    assert Condition("==", 0).holds(0) and not Condition("==", 0).holds(1)
    assert Condition(">", 85).holds(90) and not Condition(">", 85).holds(85)
    assert not Condition(">", 0).holds(float("nan"))


def test_instance_down_and_raid_templates() -> None:
    down = next(t for t in TEMPLATES if t.id == "instance-down-snmp_device")
    assert split(down.expr).parts == (
        Part('vantage_instance_up{vantage_integration="snmp_device"}', Condition("==", 0)),
    )
    raid = next(t for t in TEMPLATES if t.id == "raid-degraded-linux-host")
    assert [p.value_query for p in split(raid.expr).parts] == [
        'node_md_degraded{vantage_integration="linux_host"}',
        'node_md_disks{vantage_integration="linux_host",state="failed"}',
    ]


def test_instance_down_rules_are_recognised() -> None:
    downs = [t for t in TEMPLATES if t.id.startswith("instance-down-")]
    assert downs and all(is_instance_down(t.expr) for t in downs)
    assert is_instance_down("up < 1")
    others = [t for t in TEMPLATES if not t.id.startswith("instance-down-")]
    assert not any(is_instance_down(t.expr) for t in others)
    for expr in ("up == 1", "up > 0", "rate(up[5m]) == 0", 'up{a="b"} * 2 == 0', "upx == 0"):
        assert not is_instance_down(expr), expr
