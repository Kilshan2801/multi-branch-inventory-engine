"""Reading stock levels and the ledger, and making manual adjustments."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import InventoryItem, MovementType, Product, StockMovement, User
from app.schemas.inventory import AdjustmentCreate, AdjustmentResult, InventoryRead, StockMovementRead
from app.services import catalog_service
from app.services.audit import RequestContext, record
from app.services.stock import InsufficientStockError, StockReference, apply_movement, lock_inventory


def to_read(item: InventoryItem, product: Product) -> InventoryRead:
    return InventoryRead(
        branch_id=item.branch_id,
        product_id=item.product_id,
        sku=product.sku,
        product_name=product.name,
        on_hand=item.on_hand,
        reserved=item.reserved,
        available=item.on_hand - item.reserved,
        updated_at=item.updated_at,
    )


def list_inventory(
    db: Session, branch_id: uuid.UUID, *, search: str | None, in_stock_only: bool, limit: int, offset: int
) -> tuple[list[InventoryRead], int]:
    catalog_service.get_branch(db, branch_id)
    query = (
        select(InventoryItem, Product)
        .join(Product, Product.id == InventoryItem.product_id)
        .where(InventoryItem.branch_id == branch_id)
    )
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(Product.sku.ilike(pattern) | Product.name.ilike(pattern))
    if in_stock_only:
        query = query.where(InventoryItem.available > 0)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(Product.sku).limit(limit).offset(offset)).all()
    return [to_read(item, product) for item, product in rows], total


def get_inventory(db: Session, branch_id: uuid.UUID, product_id: uuid.UUID) -> InventoryRead:
    catalog_service.get_branch(db, branch_id)
    row = db.execute(
        select(InventoryItem, Product)
        .join(Product, Product.id == InventoryItem.product_id)
        .where(InventoryItem.branch_id == branch_id, InventoryItem.product_id == product_id)
    ).first()
    if row is None:
        # Distinguish "no such product" from "product exists, never stocked here".
        product = catalog_service.get_product(db, product_id)
        raise NotFoundError(f"No stock record for {product.sku} at this branch", code="inventory_not_found")
    return to_read(*row)


def adjust_stock(
    db: Session, branch_id: uuid.UUID, data: AdjustmentCreate, actor: User, ctx: RequestContext
) -> AdjustmentResult:
    """Manual stock change (goods received, count correction, damage).

    Does not commit: the caller decides the transaction boundary.
    """
    catalog_service.require_active_branch(db, branch_id)
    product = catalog_service.get_product(db, data.product_id)
    if data.quantity_delta > 0 and not product.is_active:
        catalog_service.require_active_products(db, [product.id])  # raises product_inactive

    adding = data.quantity_delta > 0
    items = lock_inventory(db, branch_id, [product.id], create_missing=adding)
    item = items.get(product.id)

    if item is None or item.on_hand - item.reserved < -data.quantity_delta:
        # Removing stock may only take units nobody has been promised.
        free = (item.on_hand - item.reserved) if item else 0
        raise InsufficientStockError(
            f"Cannot remove {-data.quantity_delta} units: only {free} are not reserved",
            details={
                "product_id": str(product.id),
                "on_hand": item.on_hand if item else 0,
                "reserved": item.reserved if item else 0,
                "requested_removal": -data.quantity_delta,
            },
        )

    movement = apply_movement(
        db,
        item,
        movement_type=MovementType.ADJUSTMENT,
        on_hand_delta=data.quantity_delta,
        reserved_delta=0,
        reason=data.reason,
        actor_id=actor.id,
        reference=StockReference("manual"),
    )
    db.flush()
    record(
        db,
        action="inventory.adjust",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="inventory",
        entity_id=item.id,
        branch_id=branch_id,
        details={
            "product_id": str(product.id),
            "sku": product.sku,
            "quantity_delta": data.quantity_delta,
            "on_hand_after": item.on_hand,
            "reason": data.reason,
            "movement_id": str(movement.id),
        },
    )
    db.flush()
    db.refresh(movement)
    return AdjustmentResult(inventory=to_read(item, product), movement=StockMovementRead.model_validate(movement))


def list_movements(
    db: Session,
    branch_id: uuid.UUID,
    *,
    product_id: uuid.UUID | None,
    movement_type: MovementType | None,
    reference_type: str | None,
    reference_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[StockMovement], int]:
    catalog_service.get_branch(db, branch_id)
    query = select(StockMovement).where(StockMovement.branch_id == branch_id)
    if product_id is not None:
        query = query.where(StockMovement.product_id == product_id)
    if movement_type is not None:
        query = query.where(StockMovement.movement_type == movement_type)
    if reference_type is not None:
        query = query.where(StockMovement.reference_type == reference_type)
    if reference_id is not None:
        query = query.where(StockMovement.reference_id == reference_id)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), total
