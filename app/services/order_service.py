"""Customer orders and their state machine.

    pending ──reserve──► reserved ──confirm──► confirmed ──fulfill──► fulfilled
       │                    │                     │
       └──cancel────────────┴──cancel─────────────┴──cancel──► cancelled

Stock effects (all applied through the stock engine, with ledger entries):
    reserve  : reserved += qty                 (fails as a whole if any line is short)
    fulfill  : on_hand  -= qty, reserved -= qty (the goods leave the branch)
    cancel   : reserved -= what this order still holds (nothing if it was pending)

Concurrency rules:
* The order row is locked (SELECT ... FOR UPDATE) for every transition, so two
  requests can't move the same order at the same time (e.g. cancel + fulfil).
* Lock order is always: order row first, then inventory rows sorted by product.
* Repeating a transition the order has already made (e.g. "reserve" on a
  reserved order) is a safe no-op that returns the current order: a client
  retrying after a timeout can't reserve twice.

Nothing here commits; the API layer owns the transaction.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import InvalidTransitionError, NotFoundError
from app.models import AuditOutcome, MovementType, Order, OrderItem, OrderStatus, User
from app.schemas.orders import OrderCreate, OrderItemRead, OrderRead
from app.services import catalog_service, stock
from app.services.audit import RequestContext, record, record_now
from app.services.authorization import branch_scope, ensure_branch_access

Action = Literal["reserve", "confirm", "fulfill", "cancel"]

ACTION_TARGET: dict[str, OrderStatus] = {
    "reserve": OrderStatus.RESERVED,
    "confirm": OrderStatus.CONFIRMED,
    "fulfill": OrderStatus.FULFILLED,
    "cancel": OrderStatus.CANCELLED,
}

TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.RESERVED, OrderStatus.CANCELLED}),
    OrderStatus.RESERVED: frozenset({OrderStatus.CONFIRMED, OrderStatus.CANCELLED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.FULFILLED, OrderStatus.CANCELLED}),
    OrderStatus.FULFILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

_TARGET_ACTION = {target: action for action, target in ACTION_TARGET.items()}


def allowed_actions(status: OrderStatus) -> list[str]:
    return sorted(_TARGET_ACTION[t] for t in TRANSITIONS[status])


def to_read(order: Order) -> OrderRead:
    items = [
        OrderItemRead(
            product_id=i.product_id,
            quantity=i.quantity,
            unit_price=i.unit_price,
            line_total=(i.unit_price * i.quantity).quantize(Decimal("0.01")),
        )
        for i in order.items
    ]
    return OrderRead(
        id=order.id,
        branch_id=order.branch_id,
        status=order.status,
        customer_name=order.customer_name,
        customer_contact=order.customer_contact,
        notes=order.notes,
        items=items,
        total_amount=sum((i.line_total for i in items), Decimal("0.00")),
        allowed_actions=allowed_actions(order.status),
        created_by=order.created_by,
        cancellation_reason=order.cancellation_reason,
        created_at=order.created_at,
        updated_at=order.updated_at,
        reserved_at=order.reserved_at,
        confirmed_at=order.confirmed_at,
        fulfilled_at=order.fulfilled_at,
        cancelled_at=order.cancelled_at,
    )


def _ref(order: Order) -> stock.StockReference:
    return stock.StockReference("order", order.id)


def _audit_failure(actor: User, ctx: RequestContext, action: str, order_id, branch_id, details: dict) -> None:
    # The request is about to be rolled back, so log the failed attempt on its own connection.
    record_now(
        action=f"order.{action}",
        outcome=AuditOutcome.FAILURE,
        ctx=ctx,
        actor_id=actor.id,
        entity_type="order",
        entity_id=order_id,
        branch_id=branch_id,
        details=details,
    )


# --- Queries ----------------------------------------------------------------


def get_order(db: Session, order_id: uuid.UUID, actor: User, ctx: RequestContext) -> OrderRead:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found")
    ensure_branch_access(actor, order.branch_id, ctx, action="order.read")
    return to_read(order)


def list_orders(
    db: Session,
    actor: User,
    ctx: RequestContext,
    *,
    branch_id: uuid.UUID | None,
    status: OrderStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[OrderRead], int]:
    scope = branch_scope(actor)
    if scope is not None:
        if branch_id is not None:
            ensure_branch_access(actor, branch_id, ctx, action="order.list")
        branch_id = scope

    query = select(Order)
    if branch_id is not None:
        query = query.where(Order.branch_id == branch_id)
    if status is not None:
        query = query.where(Order.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    orders = db.scalars(query.order_by(Order.created_at.desc(), Order.id).limit(limit).offset(offset)).all()
    return [to_read(o) for o in orders], total


# --- Commands ---------------------------------------------------------------


def create_order(db: Session, data: OrderCreate, actor: User, ctx: RequestContext) -> OrderRead:
    ensure_branch_access(actor, data.branch_id, ctx, action="order.create")
    catalog_service.require_active_branch(db, data.branch_id)
    products = catalog_service.require_active_products(db, [line.product_id for line in data.items])

    order = Order(
        branch_id=data.branch_id,
        customer_name=data.customer_name,
        customer_contact=data.customer_contact,
        notes=data.notes,
        created_by=actor.id,
        status=OrderStatus.PENDING,
        items=[
            OrderItem(
                product_id=line.product_id, quantity=line.quantity, unit_price=products[line.product_id].unit_price
            )  # price snapshot
            for line in data.items
        ],
    )
    db.add(order)
    db.flush()
    record(
        db,
        action="order.create",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="order",
        entity_id=order.id,
        branch_id=order.branch_id,
        details={"items": [{"product_id": str(i.product_id), "quantity": i.quantity} for i in order.items]},
    )

    if data.reserve:
        _reserve(db, order, actor, ctx)

    db.flush()
    db.refresh(order)
    return to_read(order)


def transition(
    db: Session, order_id: uuid.UUID, action: Action, actor: User, ctx: RequestContext, *, reason: str | None = None
) -> OrderRead:
    order = db.scalar(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    if order is None:
        raise NotFoundError("Order not found")
    ensure_branch_access(actor, order.branch_id, ctx, action=f"order.{action}")

    target = ACTION_TARGET[action]
    if order.status == target:
        return to_read(order)  # already done: idempotent retry, nothing changes

    if target not in TRANSITIONS[order.status]:
        details = {
            "current_status": order.status.value,
            "requested": action,
            "allowed_actions": allowed_actions(order.status),
        }
        _audit_failure(actor, ctx, action, order.id, order.branch_id, {"reason": "invalid_transition", **details})
        raise InvalidTransitionError(f"Cannot {action} an order that is {order.status.value}", details=details)

    if action == "reserve":
        _reserve(db, order, actor, ctx)
    elif action == "confirm":
        order.status = OrderStatus.CONFIRMED
        order.confirmed_at = datetime.now(UTC)
        record(
            db,
            action="order.confirm",
            ctx=ctx,
            actor_id=actor.id,
            entity_type="order",
            entity_id=order.id,
            branch_id=order.branch_id,
        )
    elif action == "fulfill":
        _fulfill(db, order, actor, ctx)
    else:
        _cancel(db, order, actor, ctx, reason)

    db.flush()
    db.refresh(order)
    return to_read(order)


def _reserve(db: Session, order: Order, actor: User, ctx: RequestContext) -> None:
    requested = {item.product_id: item.quantity for item in order.items}
    items = stock.lock_inventory(db, order.branch_id, requested)
    try:
        stock.ensure_available(items, requested, branch_id=order.branch_id)
    except stock.InsufficientStockError as exc:
        _audit_failure(
            actor, ctx, "reserve", order.id, order.branch_id, {"reason": "insufficient_stock", **exc.details}
        )
        raise

    for product_id, quantity in requested.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.RESERVATION,
            on_hand_delta=0,
            reserved_delta=quantity,
            reason=f"Reserved for order {order.id}",
            actor_id=actor.id,
            reference=_ref(order),
        )
    order.status = OrderStatus.RESERVED
    order.reserved_at = datetime.now(UTC)
    record(
        db,
        action="order.reserve",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="order",
        entity_id=order.id,
        branch_id=order.branch_id,
        details={"reserved": {str(k): v for k, v in requested.items()}},
    )


def _fulfill(db: Session, order: Order, actor: User, ctx: RequestContext) -> None:
    held = stock.reserved_by_reference(db, order.branch_id, _ref(order))
    items = stock.lock_inventory(db, order.branch_id, held)
    for product_id, quantity in held.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.FULFILLMENT,
            on_hand_delta=-quantity,
            reserved_delta=-quantity,
            reason=f"Fulfilled order {order.id}",
            actor_id=actor.id,
            reference=_ref(order),
        )
    order.status = OrderStatus.FULFILLED
    order.fulfilled_at = datetime.now(UTC)
    record(
        db,
        action="order.fulfill",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="order",
        entity_id=order.id,
        branch_id=order.branch_id,
        details={"shipped": {str(k): v for k, v in held.items()}},
    )


def _cancel(db: Session, order: Order, actor: User, ctx: RequestContext, reason: str | None) -> None:
    # Release exactly what the ledger says this order holds (nothing if it was still pending).
    held = stock.reserved_by_reference(db, order.branch_id, _ref(order))
    items = stock.lock_inventory(db, order.branch_id, held)
    for product_id, quantity in held.items():
        stock.apply_movement(
            db,
            items[product_id],
            movement_type=MovementType.RESERVATION_RELEASE,
            on_hand_delta=0,
            reserved_delta=-quantity,
            reason=f"Cancelled order {order.id}" + (f": {reason}" if reason else ""),
            actor_id=actor.id,
            reference=_ref(order),
        )
    previous = order.status
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = datetime.now(UTC)
    order.cancellation_reason = reason
    record(
        db,
        action="order.cancel",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="order",
        entity_id=order.id,
        branch_id=order.branch_id,
        details={"previous_status": previous.value, "released": {str(k): v for k, v in held.items()}, "reason": reason},
    )
