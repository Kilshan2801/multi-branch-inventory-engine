import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import Ctx, DbSession, IdempotencyKeyHeader, Paging, idempotent_response, require_permission
from app.core.permissions import Permission
from app.models import OrderStatus, User
from app.schemas.common import Page, error_responses
from app.schemas.orders import CancelRequest, OrderCreate, OrderRead
from app.services import idempotency, order_service

router = APIRouter(prefix="/orders", tags=["Orders"])

CanReadOrders = Annotated[User, Depends(require_permission(Permission.ORDER_READ))]
CanCreateOrders = Annotated[User, Depends(require_permission(Permission.ORDER_CREATE))]
CanConfirmOrders = Annotated[User, Depends(require_permission(Permission.ORDER_CONFIRM))]
CanFulfillOrders = Annotated[User, Depends(require_permission(Permission.ORDER_FULFILL))]
CanCancelOrders = Annotated[User, Depends(require_permission(Permission.ORDER_CANCEL))]


@router.post(
    "", response_model=OrderRead, status_code=status.HTTP_201_CREATED, responses=error_responses(401, 403, 409, 422)
)
def create_order(
    body: OrderCreate,
    db: DbSession,
    ctx: Ctx,
    user: CanCreateOrders,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Create an order in **pending** state, or with `"reserve": true` create it and reserve its
    stock in one transaction. If any line is short, nothing is created (409 `insufficient_stock`)."""
    outcome = idempotency.execute(
        db,
        user=user,
        key=idempotency_key,
        endpoint="POST /orders",
        payload=body,
        status_code=status.HTTP_201_CREATED,
        operation=lambda: order_service.create_order(db, body, user, ctx),
    )
    return idempotent_response(outcome)


@router.get("", response_model=Page[OrderRead], responses=error_responses(401, 403))
def list_orders(
    db: DbSession,
    ctx: Ctx,
    user: CanReadOrders,
    paging: Paging,
    branch_id: uuid.UUID | None = None,
    status: OrderStatus | None = None,
) -> Page[OrderRead]:
    """Orders, newest first. Staff and managers only see their own branch."""
    items, total = order_service.list_orders(
        db, user, ctx, branch_id=branch_id, status=status, limit=paging.limit, offset=paging.offset
    )
    return Page[OrderRead](items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{order_id}", response_model=OrderRead, responses=error_responses(401, 403, 404))
def get_order(order_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanReadOrders) -> OrderRead:
    return order_service.get_order(db, order_id, user, ctx)


def _transition(db, order_id, action, user, ctx, reason=None) -> OrderRead:
    result = order_service.transition(db, order_id, action, user, ctx, reason=reason)
    db.commit()
    return result


_TRANSITION_ERRORS = error_responses(401, 403, 404, 409)


@router.post("/{order_id}/reserve", response_model=OrderRead, responses=_TRANSITION_ERRORS)
def reserve_order(order_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanCreateOrders) -> OrderRead:
    """**pending → reserved.** Atomically reserves every line, or nothing (409 `insufficient_stock`).
    Calling it again on a reserved order changes nothing."""
    return _transition(db, order_id, "reserve", user, ctx)


@router.post("/{order_id}/confirm", response_model=OrderRead, responses=_TRANSITION_ERRORS)
def confirm_order(order_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanConfirmOrders) -> OrderRead:
    """**reserved → confirmed** (e.g. payment received). No stock change."""
    return _transition(db, order_id, "confirm", user, ctx)


@router.post("/{order_id}/fulfill", response_model=OrderRead, responses=_TRANSITION_ERRORS)
def fulfill_order(order_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanFulfillOrders) -> OrderRead:
    """**confirmed → fulfilled.** The goods leave: on hand and reserved both drop by the order quantities."""
    return _transition(db, order_id, "fulfill", user, ctx)


@router.post("/{order_id}/cancel", response_model=OrderRead, responses=_TRANSITION_ERRORS)
def cancel_order(
    order_id: uuid.UUID,
    db: DbSession,
    ctx: Ctx,
    user: CanCancelOrders,
    body: Annotated[CancelRequest | None, Body()] = None,
) -> OrderRead:
    """**pending / reserved / confirmed → cancelled.** Releases exactly the units this order holds.
    Fulfilled orders can't be cancelled."""
    return _transition(db, order_id, "cancel", user, ctx, reason=body.reason if body else None)
