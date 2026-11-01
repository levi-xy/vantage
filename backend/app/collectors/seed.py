"""Seed ``last_success_at`` from Prometheus at startup (P0-7).

The poll status lives in memory, so a restart would forget when each instance last
answered. Prometheus still has ``vantage_poll_last_success_timestamp_seconds``: one
instant query at startup reads the newest value per instance back into the store.

Best effort: if Prometheus is unreachable, errors or is slow, the field stays empty and
one line is logged. It runs as a background task, so it never delays startup or polling,
and it only fills ``last_success_at``: the state stays ``pending`` until the first cycle.
"""

import asyncio
import logging
import math
import uuid

from app.collectors.store import INSTANCE_ID, LAST_SUCCESS, MAX_SAMPLES_PER_INSTANCE, MetricsStore
from app.query.proxy import PrometheusClient, QueryError, QueryRequest

log = logging.getLogger(__name__)

SEED_TIMEOUT_SECONDS = 30.0
# One series per instance; far more instances than anyone polls.
MAX_SEED_SERIES = MAX_SAMPLES_PER_INSTANCE


def seed_query(retention: str) -> str:
    """The one query: the newest last-success time per instance over the retention.

    ``retention`` comes from validated server config (a Prometheus duration), never from a
    request.
    """
    return f"max by ({INSTANCE_ID}) (max_over_time({LAST_SUCCESS}[{retention}]))"


async def seed_last_success(
    prometheus: PrometheusClient,
    store: MetricsStore,
    retention: str,
    *,
    timeout_seconds: float = SEED_TIMEOUT_SECONDS,
) -> int:
    """Fill the store's ``last_success_at`` values; returns how many. Never raises
    (except on cancellation)."""
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await prometheus.query(
                QueryRequest(query=seed_query(retention)), max_series=MAX_SEED_SERIES
            )
    except TimeoutError:
        log.warning("last_success_at not seeded from Prometheus: timed out")
        return 0
    except QueryError as exc:  # a short, safe message
        log.warning("last_success_at not seeded from Prometheus: %s", exc.message)
        return 0
    except Exception as exc:  # never let seeding take anything else down
        log.warning("last_success_at not seeded from Prometheus: %s", type(exc).__name__)
        return 0
    seeded: dict[uuid.UUID, float] = {}
    for series in result.series:
        try:
            instance_id = uuid.UUID(series.labels[INSTANCE_ID])
            value = float(series.values[-1][1])
        except (KeyError, IndexError, ValueError):
            continue
        if math.isfinite(value):
            seeded[instance_id] = max(value, seeded.get(instance_id, value))
    taken = store.seed_last_success(seeded)
    log.info("last_success_at seeded from Prometheus for %d instance(s)", taken)
    return taken
