"""Branches and products: reference data, managed by admins, readable by everyone signed in."""

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessValidationError, ConflictError, NotFoundError
from app.models import (
    Branch,
    InventoryItem,
    Order,
    OrderStatus,
    Product,
    Transfer,
    TransferStatus,
    User,
)
from app.schemas.catalog import BranchCreate, BranchUpdate, ProductCreate, ProductUpdate
from app.services.audit import RequestContext, record

OPEN_ORDER_STATUSES = (OrderStatus.PENDING, OrderStatus.RESERVED, OrderStatus.CONFIRMED)
OPEN_TRANSFER_STATUSES = (TransferStatus.REQUESTED, TransferStatus.APPROVED, TransferStatus.DISPATCHED)


def _changes(data: Any) -> dict[str, Any]:
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise BusinessValidationError("No fields to update", code="empty_update")
    return changes


def _paginate(db: Session, query, order_by, limit: int, offset: int):
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(query.order_by(*order_by).limit(limit).offset(offset)).all()
    return list(items), total


# --- Branches ---------------------------------------------------------------


def get_branch(db: Session, branch_id: uuid.UUID) -> Branch:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise NotFoundError("Branch not found")
    return branch


def require_active_branch(db: Session, branch_id: uuid.UUID) -> Branch:
    branch = get_branch(db, branch_id)
    if not branch.is_active:
        raise BusinessValidationError(
            "Branch is inactive", code="branch_inactive", details={"branch_id": str(branch_id)}
        )
    return branch


def create_branch(db: Session, data: BranchCreate, actor: User, ctx: RequestContext) -> Branch:
    branch = Branch(**data.model_dump())
    db.add(branch)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"Branch code {data.code} already exists", code="branch_code_taken") from exc
    record(
        db,
        action="branch.create",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="branch",
        entity_id=branch.id,
        branch_id=branch.id,
        details=data.model_dump(),
    )
    db.commit()
    return branch


def list_branches(db: Session, *, is_active: bool | None, limit: int, offset: int) -> tuple[list[Branch], int]:
    query = select(Branch)
    if is_active is not None:
        query = query.where(Branch.is_active == is_active)
    return _paginate(db, query, (Branch.code,), limit, offset)


def update_branch(db: Session, branch_id: uuid.UUID, data: BranchUpdate, actor: User, ctx: RequestContext) -> Branch:
    branch = get_branch(db, branch_id)
    changes = _changes(data)

    if changes.get("is_active") is False and branch.is_active:
        _ensure_branch_can_close(db, branch)

    for field, value in changes.items():
        setattr(branch, field, value)
    action = "branch.deactivate" if changes.get("is_active") is False else "branch.update"
    record(
        db,
        action=action,
        ctx=ctx,
        actor_id=actor.id,
        entity_type="branch",
        entity_id=branch.id,
        branch_id=branch.id,
        details={"changes": changes},
    )
    db.commit()
    db.refresh(branch)
    return branch


def _ensure_branch_can_close(db: Session, branch: Branch) -> None:
    """A branch with promised stock or work in flight can't be closed: those
    reservations and transfers would be stranded."""
    reserved = db.scalar(
        select(func.coalesce(func.sum(InventoryItem.reserved), 0)).where(InventoryItem.branch_id == branch.id)
    )
    open_orders = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.branch_id == branch.id, Order.status.in_(OPEN_ORDER_STATUSES))
    )
    open_transfers = db.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(
            or_(Transfer.source_branch_id == branch.id, Transfer.destination_branch_id == branch.id),
            Transfer.status.in_(OPEN_TRANSFER_STATUSES),
        )
    )
    if reserved or open_orders or open_transfers:
        raise ConflictError(
            "Branch still has open orders, transfers or reserved stock",
            code="branch_has_open_work",
            details={"reserved_units": reserved, "open_orders": open_orders, "open_transfers": open_transfers},
        )


# --- Products ---------------------------------------------------------------


def get_product(db: Session, product_id: uuid.UUID) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError("Product not found")
    return product


def require_active_products(db: Session, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, Product]:
    """Load products, failing with one error that names every unknown or inactive id."""
    found = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(set(product_ids))))}
    missing = [str(pid) for pid in product_ids if pid not in found]
    inactive = [str(pid) for pid in product_ids if pid in found and not found[pid].is_active]
    if missing:
        raise BusinessValidationError("Unknown product(s)", code="product_not_found", details={"product_ids": missing})
    if inactive:
        raise BusinessValidationError("Inactive product(s)", code="product_inactive", details={"product_ids": inactive})
    return found


def create_product(db: Session, data: ProductCreate, actor: User, ctx: RequestContext) -> Product:
    product = Product(**data.model_dump())
    db.add(product)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"SKU {data.sku} already exists", code="sku_taken") from exc
    record(
        db,
        action="product.create",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="product",
        entity_id=product.id,
        details=data.model_dump(mode="json"),
    )
    db.commit()
    return product


def list_products(
    db: Session, *, search: str | None, is_active: bool | None, limit: int, offset: int
) -> tuple[list[Product], int]:
    query = select(Product)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(or_(Product.sku.ilike(pattern), Product.name.ilike(pattern)))
    if is_active is not None:
        query = query.where(Product.is_active == is_active)
    return _paginate(db, query, (Product.sku,), limit, offset)


def update_product(
    db: Session, product_id: uuid.UUID, data: ProductUpdate, actor: User, ctx: RequestContext
) -> Product:
    """Deactivating a product stops new orders, transfers and stock-ins for it.
    Reservations that already exist can still be confirmed, fulfilled or cancelled."""
    product = get_product(db, product_id)
    changes = _changes(data)
    for field, value in changes.items():
        setattr(product, field, value)
    action = "product.deactivate" if changes.get("is_active") is False else "product.update"
    record(
        db,
        action=action,
        ctx=ctx,
        actor_id=actor.id,
        entity_type="product",
        entity_id=product.id,
        details={"changes": {k: str(v) for k, v in changes.items()}},
    )
    db.commit()
    db.refresh(product)
    return product
