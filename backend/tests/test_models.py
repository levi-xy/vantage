import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core.db import make_session_factory, session_scope
from app.models import (
    AlertEvent,
    AlertRule,
    AlertState,
    AuditLog,
    Channel,
    ChannelType,
    Dashboard,
    Instance,
    Link,
    Role,
    Setting,
    User,
)


def _instance(name: str = "edge-fw") -> Instance:
    return Instance(type_id="generic", name=name, config={"host": "192.0.2.10"})


def test_instance_defaults_and_round_trip(session: Session) -> None:
    inst = Instance(
        type_id="generic",
        name="lab-host",
        site="home",
        tags=["rack1", "core"],
        config={"host": "192.0.2.10", "port": 9100},
        secrets=b"\x00encrypted-blob",
    )
    session.add(inst)
    session.commit()
    session.expire_all()

    got = session.get(Instance, inst.id)
    assert got is not None
    assert isinstance(got.id, uuid.UUID)
    assert got.enabled is True
    assert got.tags == ["rack1", "core"]
    assert got.config == {"host": "192.0.2.10", "port": 9100}
    assert got.secrets == b"\x00encrypted-blob"
    assert got.created_at.tzinfo is not None
    assert got.updated_at >= got.created_at


def test_instance_minimal_defaults(session: Session) -> None:
    inst = _instance()
    session.add(inst)
    session.commit()
    assert inst.tags == []
    assert inst.site is None
    assert inst.secrets is None


def test_instance_repr_hides_config_and_secrets() -> None:
    inst = Instance(
        id=uuid.uuid4(), type_id="generic", name="x", config={"host": "192.0.2.1"}, secrets=b"s"
    )
    assert "192.0.2.1" not in repr(inst)
    assert "secrets" not in repr(inst)


def test_instance_name_is_unique(session: Session) -> None:
    session.add_all([_instance("dup"), _instance("dup")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_updated_at_changes_on_update(session: Session) -> None:
    inst = _instance()
    session.add(inst)
    session.commit()
    before = inst.updated_at
    inst.name = "renamed"
    session.commit()
    assert inst.updated_at > before
    assert inst.created_at <= before


def test_datetimes_are_stored_as_utc(session: Session) -> None:
    plus_two = timezone(timedelta(hours=2))
    user = User(
        username="admin",
        password_hash="example-hash",
        role=Role.ADMIN,
        last_login_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus_two),
    )
    session.add(user)
    session.commit()
    session.expire_all()
    got = session.get(User, user.id)
    assert got is not None and got.last_login_at is not None
    assert got.last_login_at == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert got.last_login_at.utcoffset() == timedelta(0)


def test_naive_datetime_is_rejected(session: Session) -> None:
    session.add(
        User(
            username="a",
            password_hash="example-hash",
            role=Role.VIEWER,
            last_login_at=datetime(2026, 1, 1),
        )
    )
    with pytest.raises(StatementError):
        session.commit()


def test_user_repr_hides_password_hash() -> None:
    user = User(id=1, username="admin", password_hash="example-hash", role=Role.ADMIN)
    assert "example-hash" not in repr(user)


def test_username_is_unique(session: Session) -> None:
    session.add_all(
        [
            User(username="admin", password_hash="h", role=Role.ADMIN),
            User(username="admin", password_hash="h", role=Role.VIEWER),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize(
    ("table", "bad"),
    [("users", "owner"), ("alert_events", "pending"), ("channels", "email")],
)
def test_enum_columns_reject_unknown_values_at_db_level(
    engine: Engine, table: str, bad: str
) -> None:
    """The CHECK constraint holds even for writes that bypass the ORM.

    Bad values must fit the column length: Postgres enforces VARCHAR length and would
    reject a longer value with a different error before reaching the CHECK.
    """
    with make_session_factory(engine)() as s:
        if table == "alert_events":
            rule = AlertRule(name="r", expr="up == 0", severity="warning")
            s.add(rule)
            s.commit()
            stmt = text(
                "INSERT INTO alert_events (rule_id, state, started_at, summary) "
                "VALUES (:rule_id, :bad, :ts, '')"
            ).bindparams(rule_id=rule.id, bad=bad, ts=datetime.now(UTC))
        elif table == "users":
            stmt = text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('u', 'h', :bad, :ts)"
            ).bindparams(bad=bad, ts=datetime.now(UTC))
        else:
            stmt = text(
                "INSERT INTO channels (type, name, config) VALUES (:bad, 'c', '{}')"
            ).bindparams(bad=bad)
        with pytest.raises(IntegrityError):
            s.execute(stmt)
            s.commit()


def test_deleting_instance_cascades_to_links_and_scoped_rules(session: Session) -> None:
    a, b = _instance("a"), _instance("b")
    session.add_all([a, b])
    session.flush()
    rule = AlertRule(name="a down", expr="up == 0", severity="critical", instance_id=a.id)
    global_rule = AlertRule(name="any down", expr="up == 0", severity="critical")
    session.add_all(
        [Link(a_instance_id=a.id, b_instance_id=b.id, a_port="ether1"), rule, global_rule]
    )
    session.commit()
    rule_id = rule.id

    session.execute(Instance.__table__.delete().where(Instance.id == a.id))
    session.commit()
    session.expire_all()

    assert session.scalars(select(Link)).all() == []
    assert session.get(AlertRule, rule_id) is None
    assert session.get(AlertRule, global_rule.id) is not None


def test_link_requires_existing_instances(session: Session) -> None:
    session.add(Link(a_instance_id=uuid.uuid4(), b_instance_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_rule_cascades_to_events(session: Session) -> None:
    rule = AlertRule(name="r", expr="up == 0", severity="warning", channel_ids=[1, 2])
    session.add(rule)
    session.flush()
    session.add(AlertEvent(rule_id=rule.id, state=AlertState.FIRING, started_at=datetime.now(UTC)))
    session.commit()
    assert session.get(AlertRule, rule.id).channel_ids == [1, 2]  # type: ignore[union-attr]

    session.execute(AlertRule.__table__.delete().where(AlertRule.id == rule.id))
    session.commit()
    assert session.scalars(select(AlertEvent)).all() == []


def test_audit_entries_survive_user_deletion(session: Session) -> None:
    user = User(username="admin", password_hash="h", role=Role.ADMIN)
    session.add(user)
    session.flush()
    entry = AuditLog(
        user_id=user.id,
        action="update",
        target_type="instance",
        target_id=str(uuid.uuid4()),
        changes={"api_token": "<changed>"},
    )
    session.add(entry)
    session.commit()

    session.execute(User.__table__.delete().where(User.id == user.id))
    session.commit()
    session.expire_all()

    got = session.get(AuditLog, entry.id)
    assert got is not None
    assert got.user_id is None
    assert got.changes == {"api_token": "<changed>"}
    assert got.at.tzinfo is not None


def test_channel_dashboard_and_settings_round_trip(session: Session) -> None:
    session.add_all(
        [
            Channel(type=ChannelType.TELEGRAM, name="ops", config={"chat_id": "example-chat"}),
            Dashboard(name="Home", layout={"cards": [{"i": "c1", "x": 0, "y": 0}]}),
            Setting(key="net_guard.extra_cidrs", value=["10.20.0.0/16"]),
        ]
    )
    session.commit()
    session.expire_all()
    assert session.scalars(select(Channel)).one().type is ChannelType.TELEGRAM
    assert session.scalars(select(Dashboard)).one().layout["cards"][0]["i"] == "c1"
    assert session.get(Setting, "net_guard.extra_cidrs").value == ["10.20.0.0/16"]  # type: ignore[union-attr]


def test_session_scope_commits_and_rolls_back(engine: Engine) -> None:
    factory = make_session_factory(engine)

    scope = session_scope(factory)
    s = next(scope)
    s.add(_instance("kept"))
    with pytest.raises(StopIteration):
        next(scope)

    scope = session_scope(factory)
    s = next(scope)
    s.add(_instance("dropped"))
    with pytest.raises(RuntimeError):
        scope.throw(RuntimeError("boom"))

    with factory() as check:
        names = check.scalars(select(Instance.name)).all()
    assert names == ["kept"]
