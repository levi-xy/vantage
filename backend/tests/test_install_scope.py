"""Install id, the ``vantage_install`` label and query scoping (lite-install work, PR A)."""

import asyncio
import re
import uuid
from collections.abc import Iterable

import httpx
import pytest
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector, CollectorRegistry
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.alerts.selfcheck import METRICS_QUERY, SEEN_QUERY
from app.collectors.store import InstallRegistry, _InstallLabels
from app.core import install
from app.models import Setting
from app.query.proxy import PrometheusClient, QueryError, QueryRequest
from app.query.scope import QueryScope, Unsupported, rewrite
from tests import promfake
from tests.promfake import OTHER_INSTALL_ID, TEST_INSTALL_ID, query_of, unscoped

# --- the install id -----------------------------------------------------------------------


def test_the_migration_makes_the_id_and_ensure_keeps_it(engine: Engine, session: Session) -> None:
    row = session.get(Setting, install.SETTING_KEY)
    assert row is not None
    made = install.valid(row.value)
    assert made is not None and str(uuid.UUID(made)) == made
    assert install.ensure(session) == made  # never changes


def test_a_missing_or_unreadable_id_is_made_again(engine: Engine, session: Session) -> None:
    row = session.get(Setting, install.SETTING_KEY)
    assert row is not None
    session.delete(row)
    session.commit()
    first = install.ensure(session)
    assert install.valid({"id": first}) == first
    row = session.get(Setting, install.SETTING_KEY)
    assert row is not None
    row.value = {"id": "not-a-uuid"}
    session.commit()
    again = install.ensure(session)
    assert again != first and install.valid({"id": again}) == again


def test_the_backend_refuses_to_start_when_the_install_id_cannot_be_loaded(db_url: str) -> None:
    from tests.test_auth import make_client

    # No migrated database (no settings table): not a state to run in.
    with pytest.raises(Exception):  # noqa: B017 - the database driver's own error
        with make_client(db_url):
            pass


def test_load_sets_the_id_and_mode(engine: Engine, session: Session) -> None:
    found = install.load(session, "external")
    assert install.current_id() == found and install.current_mode() == "external"


# --- scoped queries: two installs, one Prometheus ----------------------------------------------


class SharedProm:
    """One Prometheus holding both installs' series: same metric names, same
    ``vantage_instance_id``, different values. It answers only what the query's matcher
    selects (and, like Prometheus, doesn't care which scrape job a series came from)."""

    def __init__(self) -> None:
        shared = {"vantage_instance_id": "00000000-0000-0000-0000-000000000001"}
        self.series: dict[str, list[tuple[dict[str, str], str]]] = {
            TEST_INSTALL_ID: [({**shared, "job": "vantage-backend"}, "11")],
            OTHER_INSTALL_ID: [({**shared, "job": "my-renamed-job"}, "22")],
        }
        self.build_info_age: dict[str, str] = {OTHER_INSTALL_ID: "5"}  # TEST has no build info

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = query_of(request)
        match = re.search(r'vantage_install="([0-9a-f-]{36})"', query)
        assert match is not None, "the shared Prometheus is only used through external scopes"
        mine = match.group(1)
        if unscoped(query) == METRICS_QUERY:
            age = self.build_info_age.get(mine)
            rows = [({}, age)] if age is not None else []
        else:
            rows = self.series[mine]
        result = [{"metric": m, "value": [1.0, v]} for m, v in rows]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    def client(self, install_id: str) -> PrometheusClient:
        promfake.extra_ids.add(install_id)
        return PrometheusClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://prom.test"),
            QueryScope(install_id, "external"),
        )


def test_two_installs_on_one_prometheus_never_mix() -> None:
    prom = SharedProm()
    a, b = prom.client(TEST_INSTALL_ID), prom.client(OTHER_INSTALL_ID)

    async def run() -> tuple[list[str], list[str], list[int], list[int]]:
        qa = await a.query(QueryRequest(query="vantage_instance_up"))
        qb = await b.query(QueryRequest(query="vantage_instance_up"))
        # The self-check's query: only B has a build info sample (under another job name).
        ca = await a.query(QueryRequest(query=METRICS_QUERY))
        cb = await b.query(QueryRequest(query=METRICS_QUERY))
        return (
            [s.values[0][1] for s in qa.series],
            [s.values[0][1] for s in qb.series],
            [len(ca.series)],
            [len(cb.series)],
        )

    values_a, values_b, build_a, build_b = asyncio.run(run())
    assert values_a == ["11"] and values_b == ["22"]
    assert build_a == [0] and build_b == [1]  # B's renamed job still counts; A sees nothing


def test_a_query_that_names_another_install_is_pinned_to_ours() -> None:
    prom = SharedProm()
    a = prom.client(TEST_INSTALL_ID)
    greedy = f'vantage_instance_up{{vantage_install="{OTHER_INSTALL_ID}"}}'

    async def run() -> list[str]:
        return [s.values[0][1] for s in (await a.query(QueryRequest(query=greedy))).series]

    assert asyncio.run(run()) == ["11"]


def test_a_query_that_cannot_be_scoped_is_refused_before_it_is_sent() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(query_of(request))
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    client = PrometheusClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://prom.test")
    )

    async def run() -> QueryError:
        with pytest.raises(QueryError) as exc:
            await client.query(QueryRequest(query='x{"a"="b"}'))
        return exc.value

    error = asyncio.run(run())
    assert (error.status_code, error.message) == (400, "query not supported")
    assert TEST_INSTALL_ID not in error.message
    assert sent == []


def test_without_a_loaded_install_id_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "_id", None)
    client = PrometheusClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: pytest.fail("a query was sent")),
            base_url="http://prom.test",
        )
    )

    async def run() -> int:
        with pytest.raises(QueryError) as exc:
            await client.query(QueryRequest(query="vector(1)"))
        return exc.value.status_code

    assert asyncio.run(run()) == 503


# --- the "unscoped query fails the test" fixture itself -----------------------------------------


def test_the_fake_prometheus_check_catches_an_unscoped_query() -> None:
    def unscoped_request(query: str) -> httpx.Request:
        return httpx.Request("POST", "http://prom.test/api/v1/query", data={"query": query})

    promfake.check_request(unscoped_request("vector(1)"))  # no selector: fine
    promfake.check_request(
        unscoped_request(f'up{{vantage_install=~"{TEST_INSTALL_ID}|"}} + on() vector(1)')
    )
    for bad in (
        "up",
        f'up{{vantage_install=~"{TEST_INSTALL_ID}|"}} + sum(up)',  # one selector forgotten
        f'up{{vantage_install="{OTHER_INSTALL_ID}"}}',  # another install's scope
        'up{vantage_install=~".*"}',
    ):
        with pytest.raises(AssertionError, match="without the install scope"):
            promfake.check_request(unscoped_request(bad))
    # A Prometheus behind a path prefix is checked too, and a query the rewriter refuses
    # counts as a violation like any other (and is remembered even if the caller swallows it).
    prefixed = httpx.Request(
        "POST", "http://prom.test/prom/api/v1/query_range", data={"query": "up"}
    )
    with pytest.raises(AssertionError):
        promfake.check_request(prefixed)
    refused = httpx.Request("POST", "http://prom.test/api/v1/query", data={"query": 'x{"a"="b"}'})
    with pytest.raises(Unsupported):
        promfake.check_request(refused)
    assert len(promfake.violations) == 6
    promfake.violations.clear()  # they were meant to happen


# --- /metrics: the label can't be set by an integration ------------------------------------------


class Evil(Collector):
    def collect(self) -> Iterable[Metric]:
        family = Metric("vantage_evil", "x", "gauge")
        family.add_sample("vantage_evil", {"vantage_install": "evil", "a": "b"}, 1.0)
        return [family]


def test_the_install_label_is_overwritten_never_trusted() -> None:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_InstallLabels(Evil(), lambda: TEST_INSTALL_ID))
    (family,) = registry.collect()
    assert [s.labels for s in family.samples] == [{"vantage_install": TEST_INSTALL_ID, "a": "b"}]


def test_nothing_is_exposed_before_the_install_id_is_loaded() -> None:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_InstallLabels(Evil(), lambda: None))
    assert list(registry.collect()) == []


def test_every_collector_registered_later_is_labelled_too() -> None:
    registry = InstallRegistry(lambda: TEST_INSTALL_ID)
    registry.register(Evil())  # e.g. the notification metrics, registered by the app
    (family,) = registry.collect()
    assert family.samples[0].labels["vantage_install"] == TEST_INSTALL_ID


def test_every_query_vantage_builds_itself_can_be_scoped() -> None:
    """The backend's own queries (the fleet and probe queries, the seed, the rule templates,
    a rule scoped to one device) must all pass the rewriter, or that feature would break."""
    from app.alerts.evaluator import POLLED_QUERY, PROBE_QUERY, SCRAPED_QUERY, scoped_query
    from app.alerts.templates import TEMPLATES
    from app.collectors.seed import seed_query

    queries = [POLLED_QUERY, SCRAPED_QUERY, PROBE_QUERY, METRICS_QUERY, SEEN_QUERY]
    queries += [seed_query("30d")]
    queries += [t.expr for t in TEMPLATES]
    queries += [scoped_query(t.expr, uuid.uuid4()) for t in TEMPLATES]
    for scope in (QueryScope(TEST_INSTALL_ID, "bundled"), QueryScope(TEST_INSTALL_ID, "external")):
        for query in queries:
            scoped = rewrite(query, scope)
            assert rewrite(scoped, scope) == scoped, query
            if query != PROBE_QUERY:
                assert scope.matcher in scoped, query
