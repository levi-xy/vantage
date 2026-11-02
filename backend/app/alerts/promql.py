"""Split an alert expression into value queries and conditions (P0-3b).

A rule like ``A > 85`` says two things: *what* is measured (``A``) and *when* it is bad
(``> 85``). Knowing both lets the evaluator check recovery positively: it runs ``A`` (the
*witness*) and sees the device's value fail the condition, instead of reading a missing
series as "fine".

The splitter is a small tokenizer that respects ``()``, ``[]``, ``{}`` and quoted strings
(with escapes). It looks only at the top level of the expression:

* parts joined by top-level ``or`` are split apart (``A > 0 or B > 0``);
* each part must hold exactly one top-level comparison with a number literal on one side
  (``A > 85``, or ``85 < A``, which is flipped to ``A > 85``).

Anything else is *unsure* and gives ``empty_result``: ``and`` / ``unless``, a comparison
between two series, ``bool``, ``or on(...)``, a comparison inside a function, matcher or
string only, no comparison at all (``absent(...)``). Such a rule resolves when its result is
empty (with a fresh source), as the rule editor warns.
"""

import math
import re
from dataclasses import dataclass
from typing import Literal

RecoveryMode = Literal["value", "empty_result"]

_COMPARISONS = ("==", "!=", ">=", "<=", ">", "<")
_FLIPPED = {"==": "==", "!=": "!=", ">=": "<=", "<=": ">=", ">": "<", "<": ">"}
_SET_OPS = ("and", "unless")
_OPEN = {"(": ")", "[": "]", "{": "}"}
_WORD = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F]+|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?![A-Za-z0-9_.])"
)
_DURATION = re.compile(r"[0-9]+[A-Za-z][A-Za-z0-9]*")
_OPERATOR = re.compile(r"==|!=|>=|<=|=~|!~|[-+*/%^<>=,@]")


class Unsure(Exception):
    """The expression can't be split with confidence."""


@dataclass(frozen=True)
class _Token:
    kind: Literal["word", "number", "op", "group", "string"]
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Condition:
    op: str
    threshold: float

    def holds(self, value: float) -> bool:
        """Whether ``value`` makes the rule's comparison true (NaN never does)."""
        if math.isnan(value):
            return False
        t = self.threshold
        return {
            "==": value == t,
            "!=": value != t,
            ">=": value >= t,
            "<=": value <= t,
            ">": value > t,
            "<": value < t,
        }[self.op]


@dataclass(frozen=True)
class Part:
    value_query: str
    condition: Condition


@dataclass(frozen=True)
class Split:
    mode: RecoveryMode
    parts: tuple[Part, ...] = ()


EMPTY_RESULT = Split("empty_result")


def _skip_string(expr: str, i: int) -> int:
    """Index just past the string starting at ``expr[i]``."""
    quote = expr[i]
    i += 1
    while i < len(expr):
        c = expr[i]
        if c == "\\" and quote != "`":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    raise Unsure("unterminated string")


def _skip_group(expr: str, i: int) -> int:
    """Index just past the bracketed group starting at ``expr[i]`` (nesting and strings
    inside respected)."""
    stack = [_OPEN[expr[i]]]
    i += 1
    while i < len(expr):
        c = expr[i]
        if c in "\"'`":
            i = _skip_string(expr, i)
            continue
        if c in _OPEN:
            stack.append(_OPEN[c])
        elif c in ")]}":
            if c != stack.pop():
                raise Unsure("unbalanced brackets")
            if not stack:
                return i + 1
        elif c == "#":  # a comment runs to the end of the line
            end = expr.find("\n", i)
            i = len(expr) if end < 0 else end
            continue
        i += 1
    raise Unsure("unbalanced brackets")


def tokenize(expr: str) -> list[_Token]:
    """Top-level tokens. A bracketed group or a string is one token."""
    tokens: list[_Token] = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":
            end = expr.find("\n", i)
            i = len(expr) if end < 0 else end
            continue
        if c in _OPEN:
            end = _skip_group(expr, i)
            tokens.append(_Token("group", expr[i:end], i, end))
            i = end
            continue
        if c in ")]}":
            raise Unsure("unbalanced brackets")
        if c in "\"'`":
            end = _skip_string(expr, i)
            tokens.append(_Token("string", expr[i:end], i, end))
            i = end
            continue
        for kind, pattern in (("number", _NUMBER), ("word", _WORD), ("word", _DURATION)):
            m = pattern.match(expr, i)
            if m:
                tokens.append(_Token(kind, m.group(0), i, m.end()))  # type: ignore[arg-type]
                i = m.end()
                break
        else:
            m = _OPERATOR.match(expr, i)
            if not m:
                raise Unsure(f"unexpected character {c!r}")
            tokens.append(_Token("op", m.group(0), i, m.end()))
            i = m.end()
    return tokens


def _is_keyword(token: _Token, *words: str) -> bool:
    return token.kind == "word" and token.text.lower() in words


def _number(tokens: list[_Token]) -> float | None:
    """The value if ``tokens`` is a single number literal (optionally signed)."""
    sign = 1.0
    if len(tokens) == 2 and tokens[0].kind == "op" and tokens[0].text in "+-":
        sign = -1.0 if tokens[0].text == "-" else 1.0
        tokens = tokens[1:]
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if token.kind == "number":
        text = token.text
        value = float(int(text, 16)) if text[:2].lower() == "0x" else float(text)
        return sign * value
    if _is_keyword(token, "inf", "nan"):
        return sign * float(token.text)
    return None


def _text(expr: str, tokens: list[_Token]) -> str:
    return expr[tokens[0].start : tokens[-1].end].strip()


def _part(expr: str, tokens: list[_Token]) -> Part:
    if not tokens:
        raise Unsure("empty operand")
    if any(_is_keyword(t, *_SET_OPS) for t in tokens):
        raise Unsure("and / unless")
    found = [i for i, t in enumerate(tokens) if t.kind == "op" and t.text in _COMPARISONS]
    if len(found) != 1:
        raise Unsure("not exactly one top-level comparison")
    at = found[0]
    op = tokens[at].text
    left, right = tokens[:at], tokens[at + 1 :]
    if not left or not right:
        raise Unsure("missing operand")
    if _is_keyword(right[0], "bool"):
        raise Unsure("bool modifier")
    if any(_is_keyword(t, "on", "ignoring", "group_left", "group_right") for t in right[:1]):
        raise Unsure("vector matching")
    right_number, left_number = _number(right), _number(left)
    if right_number is not None and left_number is None:
        query, threshold = left, right_number
    elif left_number is not None and right_number is None:
        query, threshold, op = right, left_number, _FLIPPED[op]
    else:
        raise Unsure("comparison between two series or two numbers")
    if query[-1].kind == "op":  # e.g. "A + > 5" or a dangling operator
        raise Unsure("dangling operator")
    return Part(_text(expr, query), Condition(op, threshold))


def split(expr: str) -> Split:
    """The rule's value queries and conditions, or ``empty_result`` when unsure."""
    try:
        tokens = tokenize(expr)
        groups: list[list[_Token]] = [[]]
        for i, token in enumerate(tokens):
            if _is_keyword(token, "or"):
                following = tokens[i + 1] if i + 1 < len(tokens) else None
                if following is not None and _is_keyword(following, "on", "ignoring"):
                    raise Unsure("or with vector matching")
                groups.append([])
            else:
                groups[-1].append(token)
        parts = tuple(_part(expr, group) for group in groups)
    except (Unsure, ValueError, IndexError):
        return EMPTY_RESULT
    return Split("value", parts)


def recovery_mode(expr: str) -> RecoveryMode:
    return split(expr).mode
