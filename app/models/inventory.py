"""Stock levels and the append-only stock ledger.

`inventory` holds the current numbers (one row per product per branch).
`stock_movements` holds every change ever made to those numbers. The two are
always written in the same transaction, so the ledger can be replayed to
reproduce the current state.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPrimaryKey
from app.models.enums import MovementType, db_enum


class InventoryItem(UUIDPrimaryKey, Base):
    __tablename__ = "inventory"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Derived by PostgreSQL itself: can never drift out of sync with on_hand/reserved.
    available: Mapped[int] = mapped_column(Integer, Computed("on_hand - reserved", persisted=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("branch_id", "product_id", name="uq_inventory_branch_product"),
        # The last line of defence: even buggy code cannot oversell or go negative.
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
    )


class StockMovement(UUIDPrimaryKey, CreatedAt, Base):
    """Immutable: a database trigger rejects UPDATE and DELETE on this table."""

    __tablename__ = "stock_movements"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    movement_type: Mapped[MovementType] = mapped_column(db_enum(MovementType, "movement_type"), nullable=False)
    on_hand_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    on_hand_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32))  # "order" | "transfer" | "manual"
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    __table_args__ = (
        CheckConstraint("on_hand_delta <> 0 OR reserved_delta <> 0", name="has_effect"),
        CheckConstraint("on_hand_after >= 0 AND reserved_after >= 0", name="after_non_negative"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
        Index("ix_stock_movements_branch_product_created", "branch_id", "product_id", "created_at"),
        Index("ix_stock_movements_reference", "reference_type", "reference_id"),
    )
