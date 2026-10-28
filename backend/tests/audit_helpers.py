"""Helpers shared by tests that inspect the audit log as stored."""

from sqlalchemy import Engine, text


def raw_audit_table(engine: Engine) -> str:
    """Every column of every audit_log row, as stored, in one string."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM audit_log")).all()
    return "\n".join(repr(tuple(row)) for row in rows)
