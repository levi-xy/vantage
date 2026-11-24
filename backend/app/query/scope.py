"""Scope every PromQL query to this install (lite-install work, PR A).

A Prometheus may hold other data than this install's (a user's own Prometheus, or another
Vantage install). So before a query leaves :class:`app.query.proxy.PrometheusClient`, this
module rewrites it so that **every vector selector** carries ``vantage_install`` matching
this install:

    foo                    ->  foo{vantage_install=~"<id>|"}
    foo{a="b"}             ->  foo{a="b",vantage_install=~"<id>|"}
    {__name__=~"x.*"}      ->  {__name__=~"x.*",vantage_install=~"<id>|"}

The matcher depends on the mode (``VANTAGE_PROMETHEUS_MODE``; unset, ``bundled`` only when
``VANTAGE_PROMETHEUS_URL`` is unset or the bundled default, else ``external``):

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
and the ``start()`` / ``end()`` after ``@``. The function ``info()`` is refused: Prometheus
joins ``target_info`` series inside it that no selector in the query names, so they can't
be scoped. A query without any selector (``vector(1)``,
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
_BRACKET_BODY = re.compile(r"[0-9A-Za-z.:\t\n ]*")
# Prometheus' lexer also ends a comment at \r and treats \r as space; every other control
# character is unexpected input. Only these are allowed, so the two lexers can't disagree.
_WHITESPACE = " \t\n"

_LABEL_KEYWORDS = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})
_SKIPPED_KEYWORDS = frozenset({"and", "or", "unless", "bool", "atan2"})
_COMPARISONS = frozenset({"==", "!=", ">", "<", ">=", "<="})
_AGGREGATORS = frozenset(
    {
        "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count", "count_values",
        "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    }
)  # fmt: skip
_MATCH_OPS = frozenset({"=", "!=", "=~", "!~"})


def _lex(query: str) -> list[_Tok]:
    if any((ord(ch) < 32 and ch not in _WHITESPACE) or ord(ch) == 127 for ch in query):
        raise Unsupported("control character")
    tokens: list[_Tok] = []
    i = 0
    while i < len(query):
        c = query[i]
        if c in _WHITESPACE:
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

    # Whether the previous token ends an operand (a selector, a number, a string, `)` ...):
    # then a keyword is the operator it looks like; otherwise Prometheus' grammar would read
    # `and`, `or`, `offset`, ... as a metric name, which isn't scoped here: refused.
    operand = False
    matching_end = -1  # index just after the label list of the latest on(...)/ignoring(...)
    i = 0
    while i < n:
        tok = toks[i]
        nxt = at(i + 1)
        prev = at(i - 1)
        if tok.kind in ("num", "dur", "str", "bracket"):
            operand = True
            i += 1
            continue
        if tok.kind == "op":
            if tok.text == "{":  # a selector without a metric name
                i = braces(i)
                operand = True
                continue
            if tok.text == "}":
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
                operand = True
                continue
            operand = tok.text == ")"
            i += 1
            continue

        low = tok.text.lower()
        if low == "offset":
            if not operand:
                raise Unsupported("keyword where a selector may be")
            j = i + 1
            if _is(at(j), "op", "+", "-"):
                j += 1
            if not (_is(at(j), "dur") or _is(at(j), "num")):
                raise Unsupported("unsupported offset")
            i = j + 1
        elif low in ("by", "without"):
            # Only after an aggregation: `sum by (a) (x)` or `sum(x) by (a)`.
            after_aggregator = (
                prev is not None and prev.kind == "word" and (prev.text.lower() in _AGGREGATORS)
            )
            if not (_is(prev, "op", ")") or after_aggregator):
                raise Unsupported("by/without outside an aggregation")
            operand = _is(prev, "op", ")")
            i = label_list(i + 1)
        elif low in ("on", "ignoring"):
            if operand or not (
                (prev is not None and prev.kind == "op" and prev.text not in ("(", ","))
                or _is(prev, "word", *_SKIPPED_KEYWORDS)
            ):
                raise Unsupported("matching keyword where a selector may be")
            i = label_list(i + 1)
            matching_end = i
        elif low in ("group_left", "group_right"):
            if i != matching_end:
                raise Unsupported("keyword where a selector may be")
            i = label_list(i + 1) if _is(nxt, "op", "(") else i + 1
            operand = False
        elif low in ("and", "or", "unless", "atan2"):
            if not operand:
                if low == "atan2" and _is(nxt, "op", "("):  # the function atan2(y, x)
                    i += 1
                    continue
                raise Unsupported("keyword where a selector may be")
            operand = False
            i += 1
        elif low == "bool":
            if not (prev is not None and prev.kind == "op" and prev.text in _COMPARISONS):
                raise Unsupported("keyword where a selector may be")
            i += 1
        elif low in ("inf", "nan") and not _is(nxt, "op", "{", "("):
            operand = True
            i += 1
        elif _is(nxt, "op", "("):  # a function or an aggregation
            if low == "info":
                # info() joins target_info series the query doesn't name: can't be scoped.
                raise Unsupported("info() is not supported")
            i += 1
        elif _is(nxt, "word", "by", "without"):
            if low not in _AGGREGATORS:
                raise Unsupported("by/without after a non-aggregation")
            i += 1
        elif _is(nxt, "op", "{"):
            i = braces(i + 1)
            operand = True
        else:  # a bare metric name
            edits.append((tok.end, tok.end, "{" + matcher + "}"))
            operand = True
            i += 1

    out = query
    for start, end, text in reversed(edits):
        out = out[:start] + text + out[end:]
    return out
