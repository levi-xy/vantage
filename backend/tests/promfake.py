"""Helpers for fake Prometheus servers: an unscoped query fails the test.

Every query Vantage sends must carry this install's matcher on every vector selector
(``app.query.scope``). ``conftest.py`` wraps every ``httpx.MockTransport`` handler with
:func:`check_request`, so a fake Prometheus in any test refuses, loudly, a query that
reaches it unscoped. Future code can't sneak one in.

The check is a fixed-point test: rewriting an already scoped query changes nothing, and a
selector without the matcher would change. The rewriter's own cases are in
``test_query_scope.py``.

Fakes that answer by query text use :func:`unscoped` to get the query as the caller wrote it.
"""

import re
from urllib.parse import parse_qs

import httpx

from app.core import install
from app.query import scope

TEST_INSTALL_ID = "11111111-1111-4111-8111-111111111111"
OTHER_INSTALL_ID = "22222222-2222-4222-8222-222222222222"

QUERY_PATHS = ("/api/v1/query", "/api/v1/query_range")
_ONLY = re.compile(r'\{vantage_install=~?"[0-9a-f-]{36}\|?"\}')
_ADDED = re.compile(r',vantage_install=~?"[0-9a-f-]{36}\|?"')

violations: list[str] = []
# Other installs a test runs against the same fake Prometheus (conftest clears it).
extra_ids: set[str] = set()


def unscoped(query: str) -> str:
    """``query`` without the install matcher the client added."""
    return _ADDED.sub("", _ONLY.sub("", query))


def query_of(request: httpx.Request) -> str:
    return parse_qs(request.content.decode())["query"][0]


def _own_scope(query: str) -> scope.QueryScope | None:
    """The scope a query carries, when it names one of ``extra_ids`` (tests that run several
    installs against one fake Prometheus)."""
    m = re.search(r'vantage_install(=~?)"([0-9a-f-]{36})\|?"', query)
    if m is None or m.group(2) not in extra_ids:
        return None
    return scope.QueryScope(m.group(2), "bundled" if m.group(1) == "=~" else "external")


def check_request(request: httpx.Request) -> None:
    """Raise (and remember, so a swallowed error still fails the test) when ``request`` is a
    query whose vector selectors don't all carry the matcher of the loaded install (or of
    one of ``extra_ids``)."""
    if request.url.path not in QUERY_PATHS:
        return
    query = query_of(request)
    install_id = install.current_id()
    assert install_id is not None, "a query was sent but no install id is loaded"
    own = scope.QueryScope(install_id, install.current_mode())
    if scope.rewrite(query, own) == query:
        return
    other = _own_scope(query)
    if other is not None and scope.rewrite(query, other) == query:
        return
    violations.append(query)
    raise AssertionError(f"a query reached Prometheus without the install scope: {query!r}")


_INSTALL_LABEL = re.compile(r'vantage_install="[0-9a-f-]{36}",?')


def without_install_label(exposition: str) -> str:
    """A ``/metrics`` text without the ``vantage_install`` label every sample carries, for
    tests about the other labels (the label itself has its own tests)."""
    return _INSTALL_LABEL.sub("", exposition)
