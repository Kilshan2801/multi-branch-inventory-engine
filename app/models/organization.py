"""Branches, users and products: the reference data everything else points at."""

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Numeric, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import Role, db_enum


class Branch(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "branches"

    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    __table_args__ = (CheckConstraint("length(trim(code)) > 0", name="code_not_blank"),)


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(db_enum(Role, "role"), nullable=False)
    # Admins are global (NULL); managers and staff belong to exactly one branch.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    branch: Mapped[Branch | None] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "(role = 'admin' AND branch_id IS NULL) OR (role <> 'admin' AND branch_id IS NOT NULL)",
            name="branch_matches_role",
        ),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )


class Product(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("length(trim(sku)) > 0", name="sku_not_blank"),
    )
