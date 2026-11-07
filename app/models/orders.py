"""Customer orders and inter-branch transfers (the two state machines)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import OrderStatus, TransferStatus, db_enum


class Order(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "orders"

    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        db_enum(OrderStatus, "order_status"), nullable=False, default=OrderStatus.PENDING
    )
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_contact: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)

    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin", order_by="OrderItem.id"
    )

    __table_args__ = (Index("ix_orders_branch_status", "branch_id", "status"),)


class OrderItem(UUIDPrimaryKey, Base):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)  # price at time of order

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("order_id", "product_id", name="uq_order_items_order_product"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )


class Transfer(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "transfers"

    source_branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    destination_branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[TransferStatus] = mapped_column(
        db_enum(TransferStatus, "transfer_status"), nullable=False, default=TransferStatus.REQUESTED
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Why it was rejected or cancelled.
    status_reason: Mapped[str | None] = mapped_column(Text)

    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    dispatched_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    received_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["TransferItem"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", lazy="selectin", order_by="TransferItem.id"
    )

    __table_args__ = (CheckConstraint("source_branch_id <> destination_branch_id", name="distinct_branches"),)


class TransferItem(UUIDPrimaryKey, Base):
    __tablename__ = "transfer_items"

    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    transfer: Mapped[Transfer] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("transfer_id", "product_id", name="uq_transfer_items_transfer_product"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )
