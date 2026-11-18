"""Differential test of the query rewriter against the real PromQL engine.

The rewriter has its own lexer, so a query could mean something to it and something else to
Prometheus (a comment ended by ``\\r``, a keyword that is also a metric name, ...). This test
asks Prometheus itself: ``promtool test rules`` (from the ``prom/prometheus`` image) evaluates
each rewritten query against data of this install, of another install, and of series without
the label. Whatever the rewriter let through must not return the other install's series, nor
(in ``external`` mode) the unlabelled ones; a query it refused is fine.

It needs Docker. It is skipped without it, and locally without the image already pulled
(CI pulls it: set ``CI``). ``PROMTOOL_IMAGE`` is the one the bundled compose file pins.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.query.scope import QueryScope, Unsupported, rewrite

PROMTOOL_IMAGE = "prom/prometheus:v3.2.1"
OURS = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
# Series values at 5m: ours ~6, unlabelled ~6e5, the other install ~6e12 (and their rates
# differ the same way), so any mix-up shows in sums and rates too.
NAMES = ("x", "y", "and", "or", "unless", "offset", "by", "on", "bool", "group_left", "end")
BASE_EVAL_SECONDS = 300
QUERIES_PER_FILE = 50

CORPUS = [
    # the shapes of the A5 list
    "x",
    'x{a="b"}',
    "x{}",
    '{__name__=~"x.*"}',
    '{__name__=~".+"}',
    "rate(x[5m])",
    "sum(rate(x[5m]))",
    "sum by (vantage_install) (x)",
    "sum(x) by (vantage_install)",
    "sum without (a) (x)",
    "x and on() y",
    "x or y",
    "x unless y",
    "x / on(vantage_install) group_left y",
    "x + ignoring(a) group_right() y",
    "max_over_time(rate(x[5m])[2m:1m])",
    "x offset 1m",
    "x offset -1m",
    "x @ 120",
    "x @ end()",
    "x @ start() offset 1m",
    'label_replace(x, "d", "$1", "__name__", "(.*)")',
    'label_replace(x, "d", "{vantage_install=\\"w\\"}", "__name__", "(.*)")',
    'count_values("v", x)',
    "topk(1, x)",
    "quantile(0.5, x)",
    'count({__name__=~".+"})',
    "vector(1)",
    "x > bool 0",
    "x == bool 1",
    "x atan2 y",
    # a matcher of the user's own on the install label
    'x{vantage_install="' + OTHER + '"}',
    'x{vantage_install=~".*"}',
    'x{vantage_install!="' + OURS + '"}',
    'x{vantage_install=~".+"}',
    'sum(x{vantage_install!=""})',
    # comments and whitespace
    'x # {__name__=~".+"}\n + y',
    '#c\n{__name__=~".+"}',
    '#c\r{__name__=~".+"}',
    'x#c\r+\r{__name__=~".+"}',
    "x\t+\ny",
    'x{a="b\\"c#"}',
    "x{a='single', b=`raw`}",
    # keywords that are also metric names (valid for Prometheus)
    "and",
    "or",
    "unless",
    "offset",
    "by",
    "on",
    "bool",
    "group_left",
    "end",
    "sum(or)",
    "rate(or[5m])",
    "unless offset 5m",
    "offset - 5",
    "x and and",
    "x or or",
    "(and)",
    "sum(and)",
    "sum(x, or)",
    "x + on",
    "x + end",
    "x + (offset)",
    "-offset",
    "sum by (by) (x)",
    # UTF-8 / quoted names (Prometheus 3)
    '{"x"}',
    '{"x", a="b"}',
    '{"a"="b"}',
    # odd but valid
    "(x)",
    "max_over_time((x)[5m:])",
    "max_over_time(sum(x)[5m:1m])",
    "max_over_time(x[5m:] offset 1m)",
    "-x",
    "x ^ 0.5",
    "x % 4",
    "1 + x",
    "x Offset 1m",
    "X",
    "x AND y",
    "SUM(x)",
    "Sum By (a) (x)",
]

ALLOWED_ERRORS = ("non-empty matcher",)  # e.g. `{vantage_install=~".+"}` once replaced


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    have = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "image", "inspect", PROMTOOL_IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=60,
    )
    return have.returncode == 0 or bool(os.environ.get("CI"))


pytestmark = pytest.mark.skipif(
    not _docker_ready(), reason=f"needs Docker and {PROMTOOL_IMAGE} (CI pulls it)"
)


def _series() -> list[dict[str, str]]:
    found = []
    for name in NAMES:
        for labels, values in (
            (f',vantage_install="{OURS}"', "1+1x20"),
            (f',vantage_install="{OTHER}"', "1000000000000+1000000000000x20"),
            ("", "100000+100000x20"),
        ):
            found.append({"series": f'{{__name__="{name}"{labels}}}', "values": values})
    return found


def _test_file(queries: list[str]) -> str:
    """A ``promtool test rules`` file whose every eval fails (nothing expected), so that
    promtool prints what each query returned; query ``k`` is evaluated at 5m + k seconds."""
    tests = [
        {
            "expr": query,
            "eval_time": f"{BASE_EVAL_SECONDS + k}s",
            "exp_samples": [],
        }
        for k, query in enumerate(queries)
    ]
    doc = {
        "evaluation_interval": "1m",
        "tests": [{"interval": "1m", "input_series": _series(), "promql_expr_test": tests}],
    }
    return json.dumps(doc)  # JSON is YAML; it keeps \r, quotes and unicode exact


_RESULT = re.compile(
    r"expr: .*?, time: (\d+)m(?:(\d+)s)?,\s*(err: [^\n]*|exp: [^\n]*\n\s*got: [^\n]*)"
)
_SAMPLE = re.compile(r"(\{[^}]*\}) ([^\s,]+)")


def _evaluate(tmp_path: Path, queries: list[str]) -> dict[int, tuple[str, list[tuple[str, float]]]]:
    """``{index: (error, [(labels, value)])}`` for every query that returned something."""
    out: dict[int, tuple[str, list[tuple[str, float]]]] = {}
    for first in range(0, len(queries), QUERIES_PER_FILE):
        chunk = queries[first : first + QUERIES_PER_FILE]
        (tmp_path / "t.yml").write_text(_test_file(chunk), encoding="utf-8")
        argv = [
            "docker", "run", "--rm", "-u", "0", "-v", f"{tmp_path}:/t", "--entrypoint",
            "promtool", PROMTOOL_IMAGE, "test", "rules", "/t/t.yml",
        ]  # fmt: skip
        run = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, check=False, timeout=300
        )
        text = run.stdout + run.stderr
        for m in _RESULT.finditer(text):
            seconds = int(m.group(1)) * 60 + int(m.group(2) or 0) - BASE_EVAL_SECONDS
            body = m.group(3)
            if body.startswith("err:"):
                out[first + seconds] = (body, [])
                continue
            got = body.split("got:", 1)[1]
            samples = [(s, float(v)) for s, v in _SAMPLE.findall(got)]
            out[first + seconds] = ("", samples)
        if run.returncode not in (0, 1):
            pytest.fail(f"promtool failed: {text[:500]}")
    return out


def _leaks(samples: list[tuple[str, float]], mode: str) -> list[str]:
    # ours are small; the unlabelled series (1e3..) only belong to the bundled mode
    limit = 100.0 if mode == "external" else 1e9
    return [
        f"{labels} {value}"
        for labels, value in samples
        if OTHER in labels or value >= limit or value != value
    ]


@pytest.mark.parametrize("mode", ["bundled", "external"])
def test_what_the_rewriter_lets_through_never_returns_other_data(tmp_path: Path, mode: str) -> None:
    scope = QueryScope(OURS, mode)  # type: ignore[arg-type]
    sent: list[str] = []
    for query in CORPUS:
        try:
            sent.append(rewrite(query, scope))
        except Unsupported:
            continue  # refused: nothing reaches Prometheus
    assert len(sent) > len(CORPUS) // 2  # the corpus mostly gets through
    results = _evaluate(tmp_path, sent)
    problems = []
    for k, query in enumerate(sent):
        error, samples = results.get(k, ("", []))
        if error and not any(allowed in error for allowed in ALLOWED_ERRORS):
            problems.append(f"{query!r}: {error}")
        problems += [f"{query!r} returned {leak}" for leak in _leaks(samples, mode)]
    assert not problems, "\n".join(problems)


def test_the_harness_sees_a_leak_when_a_query_is_not_scoped(tmp_path: Path) -> None:
    """The control: the same check on unrewritten queries flags them, so a green run means
    something."""
    results = _evaluate(tmp_path, ["x", "sum(rate(x[5m]))", "and"])
    for k in range(3):
        assert _leaks(results[k][1], "bundled"), k
