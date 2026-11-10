"""Stock transfers between branches and their state machine.

    requested ──approve──► approved ──dispatch──► dispatched ──receive──► received
        │                      │
        ├──reject──► rejected  └──cancel──► cancelled
        └──cancel──► cancelled

Stock effects (through the stock engine, with ledger entries):
    approve  : source reserved += qty             (fails as a whole if any line is short)
    dispatch : source on_hand -= qty, reserved -= qty   → units are now "in transit"
    receive  : destination on_hand += qty         (row created if the branch never held it)
    cancel   : source reserved -= what this transfer holds (only possible before dispatch)
    reject   : nothing (only possible before approval)

Conservation: units leave the source only at dispatch and arrive at the
destination only at receipt, both recorded against the same transfer id, so
    sum(on_hand over all branches) + units in dispatched transfers
stays constant. Stock is never silently created or destroyed.

Who may do what (in addition to role permissions):
    approve / reject / dispatch : the SOURCE branch (it's their stock)
    receive                     : the DESTINATION branch (they unpack it)
    request / cancel / read     : either branch
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import InvalidTransitionError, NotFoundError, PermissionDeniedError
from app.models import (
    AuditOutcome,
    MovementType,
    StockMovement,
    Transfer,
    TransferItem,
    TransferStatus,
    User,
)
from app.schemas.transfers import TransferCreate, TransferItemRead, TransferRead
from app.services import catalog_service, stock
from app.services.audit import RequestContext, record, record_now
from app.services.authorization import branch_scope, can_access_branch, ensure_branch_access

Action = Literal["approve", "reject", "dispatch", "receive", "cancel"]

ACTION_TARGET: dict[str, TransferStatus] = {
    "approve": TransferStatus.APPROVED,
    "reject": TransferStatus.REJECTED,
    "dispatch": TransferStatus.DISPATCHED,
    "receive": TransferStatus.RECEIVED,
    "cancel": TransferStatus.CANCELLED,
}

TRANSITIONS: dict[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.REQUESTED: frozenset({TransferStatus.APPROVED, TransferStatus.REJECTED, TransferStatus.CANCELLED}),
    TransferStatus.APPROVED: frozenset({TransferStatus.DISPATCHED, TransferStatus.CANCELLED}),
    TransferStatus.DISPATCHED: frozenset({TransferStatus.RECEIVED}),
    TransferStatus.RECEIVED: frozenset(),
    TransferStatus.REJECTED: frozenset(),
    TransferStatus.CANCELLED: frozenset(),
}

_TARGET_ACTION = {target: action for action, target in ACTION_TARGET.items()}

# Which side of the transfer must the (non-admin) user belong to?
ACTING_SIDE: dict[str, str] = {
    "approve": "source",
    "reject": "source",
    "dispatch": "source",
    "receive": "destination",
    "cancel": "either",
}


def allowed_actions(status: TransferStatus) -> list[str]:
    return sorted(_TARGET_ACTION[t] for t in TRANSITIONS[status])


def to_read(t: Transfer) -> TransferRead:
    return TransferRead(
        id=t.id,
        source_branch_id=t.source_branch_id,
        destination_branch_id=t.destination_branch_id,
        status=t.status,
        items=[TransferItemRead(product_id=i.product_id, quantity=i.quantity) for i in t.items],
        notes=t.notes,
        status_reason=t.status_reason,
        allowed_actions=allowed_actions(t.status),
        requested_by=t.requested_by,
        approved_by=t.approved_by,
        dispatched_by=t.dispatched_by,
        received_by=t.received_by,
        closed_by=t.closed_by,
        created_at=t.created_at,
        updated_at=t.updated_at,
        approved_at=t.approved_at,
        dispatched_at=t.dispatched_at,
        received_at=t.received_at,
        closed_at=t.closed_at,
    )


def _ref(t: Transfer) -> stock.StockReference:
    return stock.StockReference("transfer", t.id)


def _ensure_side(
    user: User, t_source: uuid.UUID, t_dest: uuid.UUID, side: str, ctx: RequestContext, action: str
) -> None:
    if side == "source":
        allowed = can_access_branch(user, t_source)
    elif side == "destination":
        allowed = can_access_branch(user, t_dest)
    else:
        allowed = can_access_branch(user, t_source) or can_access_branch(user, t_dest)
    if allowed:
        return
    record_now(
        action="access.denied",
        outcome=AuditOutcome.DENIED,
        ctx=ctx,
        actor_id=user.id,
        branch_id=user.branch_id,
        details={
            "attempted": action,
            "reason": f"transfer_{side}_only",
            "source_branch_id": str(t_source),
            "destination_branch_id": str(t_dest),
        },
    )
    message = {
        "source": "Only the source branch can do this",
        "destination": "Only the destination branch can do this",
        "either": "You do not belong to either branch of this transfer",
    }[side]
    raise PermissionDeniedError(message, code="branch_access_denied")


def _audit(
    db: Session, t: Transfer, action: str, actor: User, ctx: RequestContext, details: dict | None = None
) -> None:
    record(
        db,
        action=f"transfer.{action}",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="transfer",
        entity_id=t.id,
        branch_id=t.source_branch_id,
        details={"destination_branch_id": str(t.destination_branch_id), **(details or {})},
    )


# --- Queries ----------------------------------------------------------------


def get_transfer(db: Session, transfer_id: uuid.UUID, actor: User, ctx: RequestContext) -> TransferRead:
    t = db.get(Transfer, transfer_id)
    if t is None:
        raise NotFoundError("Transfer not found")
    _ensure_side(actor, t.source_branch_id, t.destination_branch_id, "either", ctx, "transfer.read")
    return to_read(t)


def list_transfers(
    db: Session,
    actor: User,
    ctx: RequestContext,
    *,
    branch_id: uuid.UUID | None,
    status: TransferStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[TransferRead], int]:
    """Transfers where `branch_id` is the source or the destination. Non-admins: own branch only."""
    scope = branch_scope(actor)
    if scope is not None:
        if branch_id is not None:
            ensure_branch_access(actor, branch_id, ctx, action="transfer.list")
        branch_id = scope

    query = select(Transfer)
    if branch_id is not None:
        query = query.where(or_(Transfer.source_branch_id == branch_id, Transfer.destination_branch_id == branch_id))
    if status is not None:
        query = query.where(Transfer.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Transfer.created_at.desc(), Transfer.id).limit(limit).offset(offset)).all()
    return [to_read(t) for t in rows], total


def in_transit_units(db: Session, product_id: uuid.UUID) -> int:
    """Units of a product that have left a source branch but not yet arrived."""
    return int(
        db.scalar(
            select(func.coalesce(func.sum(TransferItem.quantity), 0))
            .join(Transfer, Transfer.id == TransferItem.transfer_id)
            .where(TransferItem.product_id == product_id, Transfer.status == TransferStatus.DISPATCHED)
        )
    )


# --- Commands ---------------------------------------------------------------


def create_transfer(db: Session, data: TransferCreate, actor: User, ctx: RequestContext) -> TransferRead:
    _ensure_side(actor, data.source_branch_id, data.destination_branch_id, "either", ctx, "transfer.request")
    catalog_service.require_active_branch(db, data.source_branch_id)
    catalog_service.require_active_branch(db, data.destination_branch_id)
    catalog_service.require_active_products(db, [line.product_id for line in data.items])

    t = Transfer(
        source_branch_id=data.source_branch_id,
        destination_branch_id=data.destination_branch_id,
        notes=data.notes,
        requested_by=actor.id,
        status=TransferStatus.REQUESTED,
        items=[TransferItem(product_id=line.product_id, quantity=line.quantity) for line in data.items],
    )
    db.add(t)
    db.flush()
    _audit(
        db,
        t,
        "request",
        actor,
        ctx,
        {"items": [{"product_id": str(i.product_id), "quantity": i.quantity} for i in t.items]},
    )
    db.flush()
    db.refresh(t)
    return to_read(t)


def transition(
    db: Session, transfer_id: uuid.UUID, action: Action, actor: User, ctx: RequestContext, *, reason: str | None = None
) -> TransferRead:
    t = db.scalar(
        select(Transfer).where(Transfer.id == transfer_id).with_for_update().execution_options(populate_existing=True)
    )
    if t is None:
        raise NotFoundError("Transfer not found")
    _ensure_side(actor, t.source_branch_id, t.destination_branch_id, ACTING_SIDE[action], ctx, f"transfer.{action}")

    target = ACTION_TARGET[action]
    if t.status == target:
        return to_read(t)  # already done: idempotent retry

    if target not in TRANSITIONS[t.status]:
        details = {"current_status": t.status.value, "requested": action, "allowed_actions": allowed_actions(t.status)}
        record_now(
            action=f"transfer.{action}",
            outcome=AuditOutcome.FAILURE,
            ctx=ctx,
            actor_id=actor.id,
            entity_type="transfer",
            entity_id=t.id,
            branch_id=t.source_branch_id,
            details={"reason": "invalid_transition", **details},
        )
        raise InvalidTransitionError(f"Cannot {action} a transfer that is {t.status.value}", details=details)

    now = datetime.now(UTC)
    if action == "approve":
        _approve(db, t, actor, ctx)
        t.approved_by, t.approved_at = actor.id, now
    elif action == "reject":
        t.status_reason, t.closed_by, t.closed_at = reason, actor.id, now
        _audit(db, t, "reject", actor, ctx, {"reason": reason})
    elif action == "dispatch":
        _dispatch(db, t, actor, ctx)
        t.dispatched_by, t.dispatched_at = actor.id, now
    elif action == "receive":
        _receive(db, t, actor, ctx)
        t.received_by, t.received_at = actor.id, now
    else:
        _cancel(db, t, actor, ctx, reason)
        t.status_reason, t.closed_by, t.closed_at = reason, actor.id, now

    t.status = target
    db.flush()
    db.refresh(t)
    return to_read(t)


def _approve(db: Session, t: Transfer, actor: User, ctx: RequestContext) -> None:
    """Reserve the units at the source, so they can't be sold before dispatch
    and dispatch can never exceed what is actually there."""
    requested = {i.product_id: i.quantity for i in t.items}
    items = stock.lock_inventory(db, t.source_branch_id, requested)
    try:
        stock.ensure_available(items, requested, branch_id=t.source_branch_id)
    except stock.InsufficientStockError as exc:
        record_now(
            action="transfer.approve",
            outcome=AuditOutcome.FAILURE,
            ctx=ctx,
            actor_id=actor.id,
            entity_type="transfer",
            entity_id=t.id,
            branch_id=t.source_branch_id,
            details={"reason": "insufficient_stock", **exc.details},
        )
        raise
    for product_id, quantity in requested.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.RESERVATION,
            on_hand_delta=0,
            reserved_delta=quantity,
            reason=f"Reserved for transfer {t.id}",
            actor_id=actor.id,
            reference=_ref(t),
        )
    _audit(db, t, "approve", actor, ctx, {"reserved": {str(k): v for k, v in requested.items()}})


def _dispatch(db: Session, t: Transfer, actor: User, ctx: RequestContext) -> None:
    held = stock.reserved_by_reference(db, t.source_branch_id, _ref(t))
    items = stock.lock_inventory(db, t.source_branch_id, held)
    for product_id, quantity in held.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.TRANSFER_OUT,
            on_hand_delta=-quantity,
            reserved_delta=-quantity,
            reason=f"Dispatched on transfer {t.id}",
            actor_id=actor.id,
            reference=_ref(t),
        )
    _audit(db, t, "dispatch", actor, ctx, {"shipped": {str(k): v for k, v in held.items()}})


def _shipped(db: Session, t: Transfer) -> dict[uuid.UUID, int]:
    """What actually left the source, read from the ledger: exactly that amount arrives."""
    rows = db.execute(
        select(StockMovement.product_id, func.sum(-StockMovement.on_hand_delta))
        .where(
            StockMovement.branch_id == t.source_branch_id,
            StockMovement.reference_type == "transfer",
            StockMovement.reference_id == t.id,
            StockMovement.movement_type == MovementType.TRANSFER_OUT,
        )
        .group_by(StockMovement.product_id)
    ).all()
    return {product_id: int(qty) for product_id, qty in rows if qty}


def _receive(db: Session, t: Transfer, actor: User, ctx: RequestContext) -> None:
    shipped = _shipped(db, t)
    items = stock.lock_inventory(db, t.destination_branch_id, shipped, create_missing=True)
    for product_id, quantity in shipped.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.TRANSFER_IN,
            on_hand_delta=quantity,
            reserved_delta=0,
            reason=f"Received on transfer {t.id}",
            actor_id=actor.id,
            reference=_ref(t),
        )
    record(
        db,
        action="transfer.receive",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="transfer",
        entity_id=t.id,
        branch_id=t.destination_branch_id,
        details={"source_branch_id": str(t.source_branch_id), "received": {str(k): v for k, v in shipped.items()}},
    )


def _cancel(db: Session, t: Transfer, actor: User, ctx: RequestContext, reason: str | None) -> None:
    held = stock.reserved_by_reference(db, t.source_branch_id, _ref(t))
    items = stock.lock_inventory(db, t.source_branch_id, held)
    for product_id, quantity in held.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.RESERVATION_RELEASE,
            on_hand_delta=0,
            reserved_delta=-quantity,
            reason=f"Cancelled transfer {t.id}" + (f": {reason}" if reason else ""),
            actor_id=actor.id,
            reference=_ref(t),
        )
    _audit(
        db,
        t,
        "cancel",
        actor,
        ctx,
        {"previous_status": t.status.value, "released": {str(k): v for k, v in held.items()}, "reason": reason},
    )
