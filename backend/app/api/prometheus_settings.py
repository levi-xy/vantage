"""Settings -> Prometheus: optional auth from the backend to Prometheus.

``GET/PUT /settings/prometheus`` and ``POST /settings/prometheus/test`` (admin). The
URL is server config (``VANTAGE_PROMETHEUS_URL``, never editable here); only the auth is
stored. The password or token is write-only: no response, log line or audit entry ever
carries it, not even masked.
"""

import logging
import threading
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.api.deps import AdminDep, Authenticated, DbDep, SettingsDep
from app.api.instances import SecretBoxDep
from app.core import audit, key_check
from app.core.config import DEFAULT_PROMETHEUS_URL
from app.core.crypto import SecretBox
from app.models import Setting
from app.query import auth as pa
from app.query.auth import AuthKind, PrometheusAuth
from app.query.proxy import PrometheusClient, QueryError, QueryRequest

router = APIRouter()
# Saves (transaction + holder update) run one at a time, so the holder always ends on the
# last committed settings.
_save_lock = threading.Lock()
log = logging.getLogger(__name__)

MAX_USERNAME = 255
MAX_SECRET = 1024
PROBE_QUERY = "vector(1)"


class AuthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["none", "basic", "bearer"]
    username: Annotated[str, Field(min_length=1, max_length=MAX_USERNAME)] | None = None
    secret: Annotated[SecretStr, Field(min_length=1, max_length=MAX_SECRET)] | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v: str | None) -> str | None:
        # A colon would split the user name in the Basic header; controls are never valid.
        if v is not None and (":" in v or not v.isprintable()):
            raise ValueError("invalid user name")
        return v

    @field_validator("secret")
    @classmethod
    def _secret(cls, v: SecretStr | None) -> SecretStr | None:
        # The value goes into a header: no control characters, so no header injection.
        if v is not None and not v.get_secret_value().isprintable():
            raise ValueError("invalid secret")
        return v

    @model_validator(mode="after")
    def _bearer(self) -> "AuthIn":
        # A bearer token is one header token: no spaces.
        if self.kind == "bearer" and self.secret is not None:
            value = self.secret.get_secret_value()
            if not value.isascii() or any(c.isspace() for c in value):
                raise ValueError("invalid token")
        return self


class PrometheusPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth: AuthIn


class AuthOut(BaseModel):
    kind: AuthKind
    username: str | None
    secret_set: bool
    # The stored row can't be read (damaged, or sealed with another key): every query is
    # refused until an admin saves again or chooses None.
    unreadable: bool = False


class PrometheusView(BaseModel):
    url: str
    mode: Literal["bundled", "external"]
    auth: AuthOut


class ProbeResult(BaseModel):
    ok: bool
    reason: str | None  # a fixed UnreachableReason code


def _stored(db: DbDep) -> pa.StoredAuth:
    row = db.get(Setting, pa.SETTING_KEY)
    if row is None:
        return pa.StoredAuth("none", None, None)
    try:
        return pa.parse_row(row.value)
    except pa.CredentialsUnreadable:
        return pa.StoredAuth("none", None, None)  # a damaged row: the next PUT replaces it


def _unreadable(db: DbDep, box: SecretBox) -> bool:
    """Whether the stored row exists but can't be turned into request headers."""
    row = db.get(Setting, pa.SETTING_KEY)
    if row is None:
        return False
    try:
        pa.build_headers(pa.parse_row(row.value), box)
    except pa.CredentialsUnreadable:
        return True
    return False


def _view(request: Request, stored: pa.StoredAuth, unreadable: bool = False) -> PrometheusView:
    url = request.app.state.settings.prometheus_url
    return PrometheusView(
        url=url,
        # TODO(maintainer): read VANTAGE_PROMETHEUS_MODE once PR A/C add it.
        mode="bundled" if url == DEFAULT_PROMETHEUS_URL else "external",
        auth=AuthOut(
            kind=stored.kind,
            username=stored.username,
            secret_set=stored.secret_set,
            unreadable=unreadable,
        ),
    )


@router.get("/settings/prometheus")
def get_prometheus(
    request: Request, _auth: AdminDep, db: DbDep, box: SecretBoxDep
) -> PrometheusView:
    return _view(request, _stored(db), _unreadable(db, box))


@router.put("/settings/prometheus")
def put_prometheus(
    body: PrometheusPut,
    request: Request,
    auth: AdminDep,
    db: DbDep,
    box: SecretBoxDep,
    _settings: SettingsDep,
) -> PrometheusView:
    with _save_lock:
        return _save(body, request, auth, db, box)


def _save(
    body: PrometheusPut, request: Request, auth: Authenticated, db: DbDep, box: SecretBox
) -> PrometheusView:
    new = body.auth
    old = _stored(db)
    username: str | None
    blob: bytes | None
    changes: dict[str, Any] = {"kind": new.kind}
    if new.kind == "none":
        # Clearing is always allowed, also in key_mismatch mode: it is the way out when
        # the stored secret can't be read any more.
        username, blob = None, None
    else:
        if new.kind == "basic":
            username = new.username or (old.username if old.kind == "basic" else None)
            if not username:
                raise HTTPException(422, "a user name is required for basic auth")
        else:
            username = None
        if new.secret is not None:
            secret = new.secret.get_secret_value()
        elif old.kind == new.kind and old.blob is not None:
            secret = None  # unchanged
        else:
            # A secret of another kind is never reused.
            raise HTTPException(422, "a password or token is required")
        # A secret write: re-check the master key in this transaction (503 key_mismatch).
        key_check.require(db, box)
        blob = old.blob if secret is None else pa.seal_secret(box, secret)
        if username is not None:
            changes["username"] = username
        if secret is not None:
            changes["secret"] = audit.REDACTED
    kind: AuthKind = new.kind
    value = pa.row_value(kind, username, blob)
    row = db.get(Setting, pa.SETTING_KEY)
    if row is None:
        db.add(Setting(key=pa.SETTING_KEY, value=value))
    else:
        row.value = value
    audit.record(
        db,
        actor_type=audit.ActorType.USER,
        action=audit.Action.UPDATE,
        target_type="settings",
        target_id=pa.SETTING_KEY,
        user_id=auth.user.id,
        changes=changes,
    )
    stored = pa.StoredAuth(kind, username, blob)
    # Build before committing: a header that can't be built must not be stored.
    try:
        headers = pa.build_headers(stored, box)
    except pa.CredentialsUnreadable:
        # The kept secret doesn't open with this key: it can't be reused.
        raise HTTPException(422, "re-enter the password or token") from None
    db.commit()
    holder: PrometheusAuth = request.app.state.prometheus_auth
    holder.set(headers)
    return _view(request, stored)


@router.post("/settings/prometheus/test")
async def test_prometheus(request: Request, _auth: AdminDep) -> ProbeResult:
    """A ``vector(1)`` probe with the saved settings, through the same client as /query."""
    client: PrometheusClient = request.app.state.prometheus
    try:
        await client.query(QueryRequest(query=PROBE_QUERY))
    except QueryError as exc:
        return ProbeResult(ok=False, reason=exc.unreachable or "server_error")
    return ProbeResult(ok=True, reason=None)
