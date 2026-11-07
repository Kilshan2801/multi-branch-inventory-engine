"""Enumerations shared by models, schemas and services.

Stored in the database as VARCHAR + CHECK constraint (not native PostgreSQL
ENUM types), so adding a value later is a simple constraint change in a migration.
"""

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class Role(StrEnum):
    ADMIN = "admin"
    BRANCH_MANAGER = "branch_manager"
    STAFF = "staff"


class OrderStatus(StrEnum):
    PENDING = "pending"
    RESERVED = "reserved"
    CONFIRMED = "confirmed"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class TransferStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPATCHED = "dispatched"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class MovementType(StrEnum):
    """Why a stock row changed. Every change to `inventory` writes one ledger row."""

    ADJUSTMENT = "adjustment"  # manual correction / goods received (on_hand +/-)
    RESERVATION = "reservation"  # order or transfer sets units aside (reserved +)
    RESERVATION_RELEASE = "reservation_release"  # cancellation gives them back (reserved -)
    FULFILLMENT = "fulfillment"  # order handed over (on_hand -, reserved -)
    TRANSFER_OUT = "transfer_out"  # dispatched from source (on_hand -, reserved -)
    TRANSFER_IN = "transfer_in"  # received at destination (on_hand +)


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


def db_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
