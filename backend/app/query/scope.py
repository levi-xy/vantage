"""Scope every PromQL query to this install (lite-install work, PR A).

A Prometheus may hold other data than this install's (a user's own Prometheus, or another
Vantage install). So before a query leaves :class:`app.query.proxy.PrometheusClient`, this
module rewrites it so that **every vector selector** carries ``vantage_install`` matching
this install:

    foo                    ->  foo{vantage_install=~"<id>|"}
    foo{a="b"}             ->  foo{a="b",vantage_install=~"<id>|"}
    {__name__=~"x.*"}      ->  {__name__=~"x.*",vantage_install=~"<id>|"}

The matcher depends on the mode (``VANTAGE_PROMETHEUS_MODE``):

* ``external``: ``vantage_install="<id>"``, exact.
* ``bundled``: ``vantage_install=~"<id>|"``. The bundled Prometheus holds only this
  install's data, and series stored before the label existed carry none; an exact match
  would blank out the history on upgrade.

A matcher on ``vantage_install`` the query brings itself is dropped and replaced by ours: a
query can't widen its scope.

The rewriter **fails closed**. It reads the whole query with its own small lexer; anything
it can't place with confidence (a character it doesn't know, a ``by`` that doesn't follow
an aggregator, a quoted label name, an expression inside ``[...]``, ...) raises
:class:`Unsupported`, and the caller refuses the query. It never lets a query through
unscoped, and its messages never contain the install id.

What is not a selector: function and aggregation names (followed by ``(``), keywords
(``and or unless bool offset atan2 by without on ignoring group_left group_right``),
``inf`` / ``nan``, label lists, strings, durations (inside ``[...]`` and after ``offset``)
and the ``start()`` / ``end()`` after ``@``. A query without any selector (``vector(1)``,
``time()``, numbers) is returned unchanged.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Literal, NamedTuple

from app.alerts import promql
from app.core.install import LABEL, PrometheusMode


class Unsupported(Exception):
    """The query can't be scoped with confidence. The message is safe to log."""


@dataclass(frozen=True)
class QueryScope:
    install_id: str
    mode: PrometheusMode = "bundled"

    def __post_init__(self) -> None:
        # Interpolated into PromQL: only a canonical uuid is ever accepted.
        if str(uuid.UUID(self.install_id)) != self.install_id:
            raise ValueError("not an install id")

    @property
    def matcher(self) -> str:
        if self.mode == "external":
            return f'{LABEL}="{self.install_id}"'
        return f'{LABEL}=~"{self.install_id}|"'


class _Tok(NamedTuple):
    kind: Literal["word", "num", "dur", "str", "bracket", "op"]
    text: str
    start: int
    end: int


_WORD = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_HEX = re.compile(r"0[xX][0-9a-fA-F]+(?![A-Za-z0-9_.])")
_UNIT = r"(?:ms|s|m|h|d|w|y)"
_DURATION = re.compile(rf"[0-9]+(?:\.[0-9]+)?{_UNIT}(?:[0-9]+{_UNIT})*(?![A-Za-z0-9_.])")
_NUMBER = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_OPERATOR = re.compile(r"==|!=|>=|<=|=~|!~|[-+*/%^<>=,@(){}]")
_BRACKET_BODY = re.compile(r"[0-9A-Za-z.:\s]*")

_LABEL_KEYWORDS = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})
_SKIPPED_KEYWORDS = frozenset({"and", "or", "unless", "bool", "atan2"})
_AGGREGATORS = frozenset(
    {
        "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count", "count_values",
        "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    }
)  # fmt: skip
_MATCH_OPS = frozenset({"=", "!=", "=~", "!~"})


def _lex(query: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i = 0
    while i < len(query):
        c = query[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":  # a comment runs to the end of the line
            end = query.find("\n", i)
            i = len(query) if end < 0 else end
            continue
        if c in "\"'`":
            try:
                end = promql._skip_string(query, i)
            except promql.Unsure:
                raise Unsupported("unterminated string") from None
            tokens.append(_Tok("str", query[i:end], i, end))
            i = end
            continue
        if c == "[":
            end = query.find("]", i)
            if end < 0 or not _BRACKET_BODY.fullmatch(query, i + 1, end):
                raise Unsupported("unsupported range or subquery")
            tokens.append(_Tok("bracket", query[i : end + 1], i, end + 1))
            i = end + 1
            continue
        for kind, pattern in (
            ("num", _HEX),
            ("dur", _DURATION),
            ("num", _NUMBER),
            ("word", _WORD),
            ("op", _OPERATOR),
        ):
            m = pattern.match(query, i)
            if m:
                tokens.append(_Tok(kind, m.group(0), i, m.end()))  # type: ignore[arg-type]
                i = m.end()
                break
        else:
            raise Unsupported("unexpected character")
    return tokens


def _is(tok: _Tok | None, kind: str, *texts: str) -> bool:
    return tok is not None and tok.kind == kind and (not texts or tok.text.lower() in texts)


def rewrite(query: str, scope: QueryScope) -> str:
    """``query`` with every vector selector scoped to ``scope``; :class:`Unsupported` when
    that can't be done with confidence."""
    toks = _lex(query)
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement), in order
    matcher = scope.matcher
    n = len(toks)

    def at(i: int) -> _Tok | None:
        return toks[i] if 0 <= i < n else None

    def label_list(i: int) -> int:
        """Index after the ``( a, b )`` list starting at ``toks[i]``."""
        if not _is(at(i), "op", "("):
            raise Unsupported("label list expected")
        i += 1
        while not _is(at(i), "op", ")"):
            if not _is(at(i), "word"):
                raise Unsupported("unsupported label list")
            i += 1
            if _is(at(i), "op", ","):
                i += 1
            elif not _is(at(i), "op", ")"):
                raise Unsupported("unsupported label list")
        return i + 1

    def braces(i: int) -> int:
        """Scope the selector whose ``{`` is ``toks[i]``; index after its ``}``."""
        opening = toks[i]
        i += 1
        found: list[tuple[_Tok, _Tok]] = []  # (label, value) of every matcher
        comma: _Tok | None = None
        while not _is(at(i), "op", "}"):
            label, op, value = at(i), at(i + 1), at(i + 2)
            if label is None or op is None or value is None:
                raise Unsupported("unsupported matcher")
            if label.kind != "word" or op.kind != "op" or op.text not in _MATCH_OPS:
                raise Unsupported("unsupported matcher")
            if value.kind != "str":
                raise Unsupported("unsupported matcher")
            found.append((label, value))
            i += 3
            comma = None
            if _is(at(i), "op", ","):
                comma = toks[i]
                i += 1
            elif not _is(at(i), "op", "}"):
                raise Unsupported("unsupported matcher")
        closing = toks[i]
        if any(label.text == LABEL for label, _ in found):
            # A matcher on our label is replaced, never kept: rebuild the braces.
            kept = [query[label.start : value.end] for label, value in found if label.text != LABEL]
            edits.append((opening.start, closing.end, "{" + ",".join([*kept, matcher]) + "}"))
        elif not found:
            edits.append((opening.start, closing.end, "{" + matcher + "}"))
        elif comma is not None:
            edits.append((comma.end, comma.end, matcher))
        else:
            edits.append((found[-1][1].end, found[-1][1].end, "," + matcher))
        return i + 1

    i = 0
    while i < n:
        tok = toks[i]
        nxt = at(i + 1)
        if tok.kind == "op":
            if tok.text == "{":  # a selector without a metric name
                i = braces(i)
                continue
            if tok.text in ("}",):
                raise Unsupported("unbalanced braces")
            if tok.text == "@":
                j = i + 1
                if _is(at(j), "op", "+", "-"):
                    j += 1
                if _is(at(j), "num"):
                    i = j + 1
                elif (
                    _is(at(j), "word", "start", "end")
                    and _is(at(j + 1), "op", "(")
                    and _is(at(j + 2), "op", ")")
                ):
                    i = j + 3
                else:
                    raise Unsupported("unsupported @ modifier")
                continue
            i += 1
            continue
        if tok.kind != "word":
            i += 1
            continue

        low = tok.text.lower()
        if low == "offset":
            j = i + 1
            if _is(at(j), "op", "+", "-"):
                j += 1
            if not (_is(at(j), "dur") or _is(at(j), "num")):
                raise Unsupported("unsupported offset")
            i = j + 1
        elif low in _LABEL_KEYWORDS:
            if low in ("group_left", "group_right") and not _is(nxt, "op", "("):
                i += 1
            else:
                i = label_list(i + 1)
        elif low in _SKIPPED_KEYWORDS:
            i += 1
        elif low in ("inf", "nan") and not _is(nxt, "op", "{", "("):
            i += 1
        elif _is(nxt, "op", "("):  # a function or an aggregation
            i += 1
        elif _is(nxt, "word", "by", "without"):
            if low not in _AGGREGATORS:
                raise Unsupported("by/without after a non-aggregation")
            i += 1
        elif _is(nxt, "op", "{"):
            i = braces(i + 1)
        else:  # a bare metric name
            edits.append((tok.end, tok.end, "{" + matcher + "}"))
            i += 1

    out = query
    for start, end, text in reversed(edits):
        out = out[:start] + text + out[end:]
    return out
