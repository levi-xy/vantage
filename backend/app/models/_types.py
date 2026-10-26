"""Column helpers shared by the models."""

from enum import StrEnum

from sqlalchemy import Enum


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Store a StrEnum as its value in a VARCHAR with a CHECK constraint.

    Avoids native Postgres enum types, which are awkward to migrate.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=max(len(m.value) for m in enum_cls),
        values_callable=lambda cls: [m.value for m in cls],
        validate_strings=True,
    )
