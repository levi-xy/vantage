"""The query rewriter (app.query.scope): every vector selector gets the install matcher, and
anything it can't place is refused, never sent unscoped."""

import pytest

from app.query.scope import QueryScope, Unsupported, rewrite
from tests.promfake import OTHER_INSTALL_ID, TEST_INSTALL_ID

BUNDLED = QueryScope(TEST_INSTALL_ID, "bundled")
EXTERNAL = QueryScope(TEST_INSTALL_ID, "external")
M = f'vantage_install=~"{TEST_INSTALL_ID}|"'
X = f'vantage_install="{TEST_INSTALL_ID}"'


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("foo", "foo{M}"),
        ('foo{a="b"}', 'foo{a="b",M}'),
        ('foo{a="b",}', 'foo{a="b",M}'),
        ("foo{}", "foo{M}"),
        ('{__name__=~"x.*"}', '{__name__=~"x.*",M}'),
        ('{__name__=~"x.*",job="a"}', '{__name__=~"x.*",job="a",M}'),
        ("rate(x[5m])", "rate(x{M}[5m])"),
        ("sum by (a) (x)", "sum by (a) (x{M})"),
        ("sum(x) by (a, b)", "sum(x{M}) by (a, b)"),
        ("sum without (a) (rate(x[1m]))", "sum without (a) (rate(x{M}[1m]))"),
        ("x and on(a) y", "x{M} and on(a) y{M}"),
        ("x / ignoring(a) group_left(b) y", "x{M} / ignoring(a) group_left(b) y{M}"),
        ("x / on(a) group_left y", "x{M} / on(a) group_left y{M}"),
        ("x unless y or z", "x{M} unless y{M} or z{M}"),
        ("x > bool 0", "x{M} > bool 0"),
        (
            "max_over_time(rate(x[5m])[1h:1m])",
            "max_over_time(rate(x{M}[5m])[1h:1m])",
        ),
        ("x offset 5m", "x{M} offset 5m"),
        ("x offset -1h30m", "x{M} offset -1h30m"),
        ("x @ end()", "x{M} @ end()"),
        ("x @ 1700000000.5 offset 5m", "x{M} @ 1700000000.5 offset 5m"),
        ('x{a="b"}[5m] @ start()', 'x{a="b",M}[5m] @ start()'),
        # strings that look like selectors are strings
        (
            'label_replace(up, "dst", "$1", "src", "foo{a=\\"b\\"}")',
            'label_replace(up{M}, "dst", "$1", "src", "foo{a=\\"b\\"}")',
        ),
        ("label_join(a, 'x', ',', 'b', 'c')", "label_join(a{M}, 'x', ',', 'b', 'c')"),
        ("up # foo{bar}\n + bar", "up{M} # foo{bar}\n + bar{M}"),
        ("1 + inf", "1 + inf"),
        ("x * Inf * NaN", "x{M} * Inf * NaN"),
        ("atan2(x, y)", "atan2(x{M}, y{M})"),
        ("x atan2 y", "x{M} atan2 y{M}"),
        ("recording:rule:5m", "recording:rule:5m{M}"),
        ('count_values("v", x)', 'count_values("v", x{M})'),
        ('topk(3, rate(x{a=~"b|c"}[5m]))', 'topk(3, rate(x{a=~"b|c",M}[5m]))'),
        ("quantile(0.9, x)", "quantile(0.9, x{M})"),
        # shapes the web app's cards send
        (
            '{vantage_instance_id="a-b",__name__=~"x_(in|out)"}',
            '{vantage_instance_id="a-b",__name__=~"x_(in|out)",M}',
        ),
        (
            '8 * rate(net_bytes_recv{vantage_instance_id="a",if_name!~"lo|docker.*"}[5m])',
            '8 * rate(net_bytes_recv{vantage_instance_id="a",if_name!~"lo|docker.*",M}[5m])',
        ),
        ('up{vantage_instance_id="a"} == 1', 'up{vantage_instance_id="a",M} == 1'),
        ("sum(x) or vector(0)", "sum(x{M}) or vector(0)"),
        (
            "histogram_quantile(0.9, sum by (le) (rate(h_bucket[5m])))",
            "histogram_quantile(0.9, sum by (le) (rate(h_bucket{M}[5m])))",
        ),
    ],
)
def test_every_selector_is_scoped(query: str, expected: str) -> None:
    assert rewrite(query, BUNDLED) == expected.replace("{M}", "{" + M + "}").replace(
        ",M}", "," + M + "}"
    )


def test_external_mode_matches_exactly() -> None:
    assert rewrite('x{a="b"}', EXTERNAL) == f'x{{a="b",{X}}}'
    assert rewrite("sum(x)", EXTERNAL) == f"sum(x{{{X}}})"


@pytest.mark.parametrize("query", ["vector(1)", "time()", "1 + 2", "scalar(vector(3))", '"s"'])
def test_queries_without_a_selector_pass_unchanged(query: str) -> None:
    assert rewrite(query, BUNDLED) == query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ('x{vantage_install="other"}', f"x{{{M}}}"),
        ('x{vantage_install=~".*"}', f"x{{{M}}}"),
        ('x{vantage_install!="a",job="b"}', f'x{{job="b",{M}}}'),
        ('x{job="b",vantage_install!~"a"}', f'x{{job="b",{M}}}'),
        ('{vantage_install=~".+",__name__="x"}', f'{{__name__="x",{M}}}'),
        ('sum(x{vantage_install="a"}) / y{vantage_install=~"b"}', f"sum(x{{{M}}}) / y{{{M}}}"),
    ],
)
def test_a_users_own_install_matcher_is_replaced_never_kept(query: str, expected: str) -> None:
    assert rewrite(query, BUNDLED) == expected


def test_the_result_scopes_a_rewritten_query_again_to_itself() -> None:
    q = 'sum by (a) (rate(x{b="c"}[5m])) / on(a) y offset 1h'
    once = rewrite(q, BUNDLED)
    assert rewrite(once, BUNDLED) == once
    # ... and a query scoped to another install is re-scoped to ours
    other = rewrite(q, QueryScope(OTHER_INSTALL_ID, "external"))
    assert OTHER_INSTALL_ID not in rewrite(other, BUNDLED)


@pytest.mark.parametrize(
    "query",
    [
        'x{a="b"',  # unbalanced
        'x{a="b}',  # unterminated string
        "x}",  # stray brace
        "x{a=b}",  # unquoted value
        'x{"a"="b"}',  # quoted label name
        '{"x"}',  # quoted metric name
        "x{a}",  # no operator
        "x{a=~}",
        "x[5m+1m]",  # an expression in a range
        "x[rate(y[5m])]",
        "x[5m",  # unterminated range
        "x @",  # @ without an argument
        "x @ y",  # @ with a selector
        "x offset y",  # offset with a selector
        "x offset",
        "x by (a)",  # by after a non-aggregation
        "sum by a (x)",  # label list without parentheses
        "sum by (a, 1) (x)",  # something other than a label in the list
        "sum by (a, rate(y)) (x)",
        "x ä",  # an unexpected character
        "x $ y",
        "x ! y",
        "x ; y",
    ],
)
def test_what_cannot_be_placed_is_refused(query: str) -> None:
    with pytest.raises(Unsupported):
        rewrite(query, BUNDLED)


def test_nothing_hides_a_selector_from_the_rewriter() -> None:
    # Every way of writing a selector the lexer could mistake for something else:
    for query in (
        "x_by",  # a name that starts with a keyword
        "by_x",
        "offsetx",
        "sum_x",
        "sum",  # a metric named like an aggregator
        "end",  # start/end are metric names outside an @ modifier
        'start{a="b"}',
        "rate",  # a metric named like a function, not followed by (
    ):
        out = rewrite(query, BUNDLED)
        assert out.count(M) == 1, query


def test_error_messages_never_name_the_install() -> None:
    for query in ("x{", 'x{"a"="b"}', "x by (a)", "x[5m+1m]"):
        with pytest.raises(Unsupported) as exc:
            rewrite(query, BUNDLED)
        assert TEST_INSTALL_ID not in str(exc.value)


def test_a_scope_takes_only_a_canonical_uuid() -> None:
    for bad in (
        "",
        "abc",
        'x"} or {a="',
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        TEST_INSTALL_ID + "x",
    ):
        with pytest.raises(ValueError):
            QueryScope(bad)
