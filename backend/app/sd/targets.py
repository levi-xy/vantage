"""``GET /sd/targets``: Prometheus HTTP service discovery (Contract 3, Contract 4, M2.3).

Returns every enabled instance's ``scrape_targets()`` in the HTTP SD format::

    [{"targets": ["10.0.0.5:9100"],
      "labels": {"__metrics_path__": "/metrics", "__scheme__": "http",
                 "vantage_instance_id": "...", "vantage_instance": "nas-1",
                 "vantage_integration": "linux_host", "vantage_site": "lab"}}]

Only Prometheus calls this, with the bearer token (:mod:`app.core.prometheus_auth`).

Prometheus connects to these targets itself, outside ``net_guard``. So every target is
checked by the instance's guard here (Contract 5) and left out if it is refused: a
denied address (loopback, link-local, metadata, ...), a host that doesn't resolve right
now, or a cloud host over plain http.

One broken instance (unknown type, undecryptable secrets, a config that no longer
validates, a raising ``scrape_targets``, a malformed target) is left out and logged by
id and reason only; it never takes the others down. Nothing from the config is logged
or returned except the target addresses and the four standard labels.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbDep
from app.core.crypto import DecryptionError, SecretBox
from app.core.net_guard import NetGuard, NetGuardError
from app.core.prometheus_auth import PrometheusAuthDep
from app.integrations import instance_config as ic
from app.integrations.base import ScrapeTarget
from app.integrations.registry import Registry
from app.models import Instance

router = APIRouter()
log = logging.getLogger(__name__)

# Per-target time limit on the guard's DNS check. Prometheus' own SD timeout is longer.
RESOLVE_TIMEOUT_SECONDS = 5.0
_MAX_PATH_LENGTH = 2048
_FORBIDDEN_ADDRESS_CHARS = frozenset("/\\@?#%")


class TargetGroup(BaseModel):
    targets: list[str]
    labels: dict[str, str]


@dataclass(frozen=True)
class _Candidate:
    instance_id: uuid.UUID
    labels: dict[str, str]
    target: ScrapeTarget
    host: str
    port: int
    guard: NetGuard


def standard_labels(row: Instance) -> dict[str, str]:
    """The four labels every Vantage series carries (Contract 1)."""
    return {
        "vantage_instance_id": str(row.id),
        "vantage_instance": row.name,
        "vantage_integration": row.type_id,
        # TODO(maintainer): an instance without a site gets "", which Prometheus treats
        # as the label being absent. Should it be a fixed value (e.g. "default") instead?
        "vantage_site": row.site or "",
    }


def split_address(address: str) -> tuple[str, int] | None:
    """``(host, port)`` for a plain ``host:port`` / ``[v6]:port``, else ``None``."""
    if (
        not address
        or len(address) > 300
        or not address.isascii()
        or not address.isprintable()
        or " " in address
        or _FORBIDDEN_ADDRESS_CHARS & set(address)
    ):
        return None
    try:
        url = httpx.URL(f"http://{address}")
    except httpx.InvalidURL:
        return None
    if not url.host or url.port is None or url.userinfo:
        return None
    return url.host, url.port


def _valid_path(path: str) -> bool:
    return (
        path.startswith("/")
        and len(path) <= _MAX_PATH_LENGTH
        and path.isascii()
        and path.isprintable()
        and not any(c in path for c in " ?#")
    )


def _instance_candidates(
    row: Instance, registry: Registry, box: SecretBox, factory: ic.NetGuardFactory
) -> list[_Candidate]:
    """The well-formed targets of one instance, or ``[]`` if it can't be used."""
    cls = registry.get(row.type_id)
    if cls is None:
        log.warning("sd: instance %s has an unknown integration type; skipped", row.id)
        return []
    try:
        secrets = ic.decrypt(box, row.id, row.secrets)
    except DecryptionError:
        log.warning("sd: stored secrets of instance %s can't be decrypted; skipped", row.id)
        return []
    try:
        model = cls.config_schema.model_validate({**row.config, **secrets})
    except ValidationError:
        log.warning("sd: stored config of instance %s no longer validates; skipped", row.id)
        return []
    guard = factory(cls)
    ctx = ic.build_context(instance_id=row.id, name=row.name, config=model, net_guard=guard)
    try:
        targets = cls().scrape_targets(ctx)
    except Exception as exc:
        # The message could hold config values: log the type only.
        log.warning("sd: scrape_targets of instance %s raised %s", row.id, type(exc).__name__)
        return []

    labels = standard_labels(row)
    found: list[_Candidate] = []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, ScrapeTarget):
            log.warning("sd: instance %s returned a non-ScrapeTarget; skipped", row.id)
            continue
        hostport = split_address(target.address)
        if hostport is None or not _valid_path(target.metrics_path):
            log.warning("sd: instance %s returned a malformed target; skipped", row.id)
            continue
        found.append(_Candidate(row.id, labels, target, *hostport, guard))
    return found


async def _allowed(candidate: _Candidate) -> bool:
    """Whether the guard lets this target through (Contract 5). Fails closed."""
    guard = candidate.guard
    try:
        cloud = guard.is_cloud_host(candidate.host)
        if cloud and candidate.target.scheme != "https":
            raise NetGuardError("cloud hosts must be reached over https")
        async with asyncio.timeout(RESOLVE_TIMEOUT_SECONDS):
            await guard.resolve(candidate.host, candidate.port, cloud_host=cloud)
    except (NetGuardError, TimeoutError):
        # The guard's message names the host; keep it out of the log all the same.
        log.warning(
            "sd: a target of instance %s was refused by the network guard; skipped",
            candidate.instance_id,
        )
        return False
    return True


def _group(candidates: list[_Candidate]) -> list[TargetGroup]:
    """One group per instance, metrics path and scheme, in first-seen order."""
    groups: dict[tuple[uuid.UUID, str, str], TargetGroup] = {}
    for c in candidates:
        key = (c.instance_id, c.target.metrics_path, c.target.scheme)
        group = groups.get(key)
        if group is None:
            group = groups[key] = TargetGroup(
                targets=[],
                labels={
                    "__metrics_path__": c.target.metrics_path,
                    "__scheme__": c.target.scheme,
                    **c.labels,
                },
            )
        if c.target.address not in group.targets:
            group.targets.append(c.target.address)
    return list(groups.values())


async def discover(
    db: Session, registry: Registry, box: SecretBox, factory: ic.NetGuardFactory
) -> list[TargetGroup]:
    """Target groups for every enabled instance, ordered by instance name."""
    rows = db.scalars(select(Instance).where(Instance.enabled).order_by(Instance.name))
    candidates = [c for row in rows for c in _instance_candidates(row, registry, box, factory)]
    allowed = await asyncio.gather(*(_allowed(c) for c in candidates))
    return _group([c for c, ok in zip(candidates, allowed, strict=True) if ok])


def _state(request: Request) -> tuple[Registry, SecretBox, ic.NetGuardFactory]:
    state = request.app.state
    return state.registry, state.secret_box, state.net_guard_factory


StateDep = Annotated[tuple[Registry, SecretBox, ic.NetGuardFactory], Depends(_state)]


@router.get("/sd/targets", include_in_schema=False)
async def sd_targets(
    _auth: PrometheusAuthDep, db: DbDep, state: StateDep, response: Response
) -> list[TargetGroup]:
    response.headers["Cache-Control"] = "no-store"
    return await discover(db, *state)
