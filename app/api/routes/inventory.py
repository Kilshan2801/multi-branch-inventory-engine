import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Ctx, DbSession, Paging, require_permission
from app.core.permissions import Permission
from app.models import AuditOutcome, MovementType, User
from app.schemas.common import Page, error_responses
from app.schemas.inventory import (
    AdjustmentCreate,
    AdjustmentResult,
    AuditLogRead,
    InventoryRead,
    StockMovementRead,
)
from app.services import audit_query, inventory_service
from app.services.authorization import ensure_branch_access

router = APIRouter(prefix="/branches/{branch_id}", tags=["Inventory"])
audit = APIRouter(prefix="/audit-logs", tags=["Audit"])

CanReadInventory = Annotated[User, Depends(require_permission(Permission.INVENTORY_READ))]
CanAdjustInventory = Annotated[User, Depends(require_permission(Permission.INVENTORY_ADJUST))]
CanReadAudit = Annotated[User, Depends(require_permission(Permission.AUDIT_READ))]


@router.get("/inventory", response_model=Page[InventoryRead], responses=error_responses(401, 403, 404))
def list_inventory(
    branch_id: uuid.UUID,
    db: DbSession,
    ctx: Ctx,
    user: CanReadInventory,
    paging: Paging,
    search: Annotated[str | None, Query(max_length=100, description="Matches SKU or product name")] = None,
    in_stock_only: bool = False,
) -> Page[InventoryRead]:
    """Stock levels (on hand / reserved / available) for every product held at a branch."""
    ensure_branch_access(user, branch_id, ctx, action="inventory.read")
    items, total = inventory_service.list_inventory(
        db, branch_id, search=search, in_stock_only=in_stock_only, limit=paging.limit, offset=paging.offset
    )
    return Page[InventoryRead](items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/inventory/{product_id}", response_model=InventoryRead, responses=error_responses(401, 403, 404))
def get_inventory(
    branch_id: uuid.UUID, product_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanReadInventory
) -> InventoryRead:
    ensure_branch_access(user, branch_id, ctx, action="inventory.read")
    return inventory_service.get_inventory(db, branch_id, product_id)


@router.post(
    "/inventory/adjustments",
    response_model=AdjustmentResult,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 404, 409, 422),
)
def adjust_inventory(
    branch_id: uuid.UUID, body: AdjustmentCreate, db: DbSession, ctx: Ctx, user: CanAdjustInventory
) -> AdjustmentResult:
    """Manual stock change with a mandatory reason: goods received (+), count corrections (+/-),
    damage or loss (-). Units that are reserved can't be removed (409 `insufficient_stock`)."""
    ensure_branch_access(user, branch_id, ctx, action="inventory.adjust")
    result = inventory_service.adjust_stock(db, branch_id, body, user, ctx)
    db.commit()
    return result


@router.get("/stock-movements", response_model=Page[StockMovementRead], responses=error_responses(401, 403, 404))
def list_stock_movements(
    branch_id: uuid.UUID,
    db: DbSession,
    ctx: Ctx,
    user: CanReadInventory,
    paging: Paging,
    product_id: uuid.UUID | None = None,
    movement_type: MovementType | None = None,
    reference_type: Annotated[str | None, Query(description="order, transfer or manual")] = None,
    reference_id: uuid.UUID | None = None,
) -> Page[StockMovementRead]:
    """The stock ledger of a branch, newest first: every change, who made it, why, and the resulting levels."""
    ensure_branch_access(user, branch_id, ctx, action="inventory.read")
    items, total = inventory_service.list_movements(
        db,
        branch_id,
        product_id=product_id,
        movement_type=movement_type,
        reference_type=reference_type,
        reference_id=reference_id,
        limit=paging.limit,
        offset=paging.offset,
    )
    return Page[StockMovementRead](
        items=[StockMovementRead.model_validate(m) for m in items],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@audit.get("", response_model=Page[AuditLogRead], responses=error_responses(401, 403))
def list_audit_logs(
    db: DbSession,
    ctx: Ctx,
    user: CanReadAudit,
    paging: Paging,
    action: Annotated[str | None, Query(description="Exact action or prefix, e.g. `order` or `auth.login`")] = None,
    outcome: AuditOutcome | None = None,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    branch_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Page[AuditLogRead]:
    """Security and inventory audit trail, newest first. Branch managers see their own branch only."""
    items, total = audit_query.list_audit_logs(
        db,
        user,
        ctx,
        action=action,
        outcome=outcome,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        branch_id=branch_id,
        since=since,
        until=until,
        limit=paging.limit,
        offset=paging.offset,
    )
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(a) for a in items], total=total, limit=paging.limit, offset=paging.offset
    )
