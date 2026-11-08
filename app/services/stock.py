"""The stock engine: the ONLY code that changes inventory numbers.

Every stock change in the system (manual adjustment, order reservation,
cancellation, fulfilment, transfer dispatch/receipt) goes through this module,
which guarantees three things:

1. Rows are locked before they are read.
   `lock_inventory` runs SELECT ... FOR UPDATE, so a second transaction that
   wants the same (branch, product) row waits until the first one commits or
   rolls back. Nobody ever decides based on a number another transaction is
   about to change.

2. Rows are always locked in the same order (sorted by product id).
   Two transactions that both need products A and B lock A first, then B, so
   they can never each hold one lock while waiting for the other (deadlock).

3. Every change writes a ledger row (stock_movements) in the same transaction.

The caller owns the transaction: nothing here commits. If the caller raises
after some movements were applied, the whole transaction rolls back and no
partial change survives.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models import InventoryItem, MovementType, StockMovement


class InsufficientStockError(ConflictError):
    code = "insufficient_stock"


@dataclass(frozen=True)
class StockReference:
    """What caused a movement, e.g. ("order", <order id>)."""

    type: str
    id: uuid.UUID | None = None


def lock_inventory(
    db: Session, branch_id: uuid.UUID, product_ids: Iterable[uuid.UUID], *, create_missing: bool = False
) -> dict[uuid.UUID, InventoryItem]:
    """Lock and return the inventory rows of `product_ids` at `branch_id`.

    With create_missing=True, rows that don't exist yet are created at zero
    first (needed when stock arrives at a branch for the first time).
    Products without a row are simply absent from the result otherwise.
    """
    ids = sorted(set(product_ids))
    if not ids:
        return {}

    if create_missing:
        db.execute(
            pg_insert(InventoryItem)
            .values([{"id": uuid.uuid4(), "branch_id": branch_id, "product_id": pid} for pid in ids])
            .on_conflict_do_nothing(constraint="uq_inventory_branch_product")
        )

    rows = db.scalars(
        select(InventoryItem)
        .where(InventoryItem.branch_id == branch_id, InventoryItem.product_id.in_(ids))
        .order_by(InventoryItem.product_id)
        .with_for_update()
        # Overwrite any copy already in the session with the freshly locked values.
        .execution_options(populate_existing=True)
    ).all()
    return {row.product_id: row for row in rows}


def ensure_available(
    items: Mapping[uuid.UUID, InventoryItem], requested: Mapping[uuid.UUID, int], *, branch_id: uuid.UUID
) -> None:
    """Raise InsufficientStockError listing EVERY short line, before anything is changed."""
    shortages = []
    for product_id, quantity in requested.items():
        item = items.get(product_id)
        available = (item.on_hand - item.reserved) if item else 0
        if quantity > available:
            shortages.append({"product_id": str(product_id), "requested": quantity, "available": available})
    if shortages:
        raise InsufficientStockError(
            "Not enough available stock", details={"branch_id": str(branch_id), "shortages": shortages}
        )


def apply_movement(
    db: Session,
    item: InventoryItem,
    *,
    movement_type: MovementType,
    on_hand_delta: int,
    reserved_delta: int,
    reason: str,
    actor_id: uuid.UUID,
    reference: StockReference,
) -> StockMovement:
    """Change one locked inventory row and record the change in the ledger."""
    new_on_hand = item.on_hand + on_hand_delta
    new_reserved = item.reserved + reserved_delta
    if new_on_hand < 0 or new_reserved < 0 or new_reserved > new_on_hand:
        # Callers check first and give a better message; this is the second line of defence.
        # (The database CHECK constraints are the third.)
        raise InsufficientStockError(
            "Stock change would make inventory invalid",
            details={
                "product_id": str(item.product_id),
                "on_hand": item.on_hand,
                "reserved": item.reserved,
                "on_hand_delta": on_hand_delta,
                "reserved_delta": reserved_delta,
            },
        )

    item.on_hand = new_on_hand
    item.reserved = new_reserved
    movement = StockMovement(
        branch_id=item.branch_id,
        product_id=item.product_id,
        movement_type=movement_type,
        on_hand_delta=on_hand_delta,
        reserved_delta=reserved_delta,
        on_hand_after=new_on_hand,
        reserved_after=new_reserved,
        reason=reason,
        reference_type=reference.type,
        reference_id=reference.id,
        actor_id=actor_id,
    )
    db.add(movement)
    return movement
