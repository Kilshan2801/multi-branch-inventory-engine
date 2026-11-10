import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import Ctx, DbSession, IdempotencyKeyHeader, Paging, idempotent_response, require_permission
from app.core.permissions import Permission
from app.models import TransferStatus, User
from app.schemas.common import Page, error_responses
from app.schemas.transfers import RejectRequest, TransferCancelRequest, TransferCreate, TransferRead
from app.services import idempotency, transfer_service

router = APIRouter(prefix="/transfers", tags=["Transfers"])

CanRead = Annotated[User, Depends(require_permission(Permission.TRANSFER_READ))]
CanRequest = Annotated[User, Depends(require_permission(Permission.TRANSFER_REQUEST))]
CanApprove = Annotated[User, Depends(require_permission(Permission.TRANSFER_APPROVE))]
CanDispatch = Annotated[User, Depends(require_permission(Permission.TRANSFER_DISPATCH))]
CanReceive = Annotated[User, Depends(require_permission(Permission.TRANSFER_RECEIVE))]
CanCancel = Annotated[User, Depends(require_permission(Permission.TRANSFER_CANCEL))]

_TRANSITION_ERRORS = error_responses(401, 403, 404, 409)


@router.post(
    "", response_model=TransferRead, status_code=status.HTTP_201_CREATED, responses=error_responses(401, 403, 422)
)
def create_transfer(
    body: TransferCreate,
    db: DbSession,
    ctx: Ctx,
    user: CanRequest,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Request stock to move from one branch to another (status **requested**).
    The requester must belong to the source or the destination branch. No stock moves yet."""
    outcome = idempotency.execute(
        db,
        user=user,
        key=idempotency_key,
        endpoint="POST /transfers",
        payload=body,
        status_code=status.HTTP_201_CREATED,
        operation=lambda: transfer_service.create_transfer(db, body, user, ctx),
    )
    return idempotent_response(outcome)


@router.get("", response_model=Page[TransferRead], responses=error_responses(401, 403))
def list_transfers(
    db: DbSession,
    ctx: Ctx,
    user: CanRead,
    paging: Paging,
    branch_id: uuid.UUID | None = None,
    status: TransferStatus | None = None,
) -> Page[TransferRead]:
    """Transfers in or out of a branch, newest first. Use `status=dispatched` to see stock in transit."""
    items, total = transfer_service.list_transfers(
        db, user, ctx, branch_id=branch_id, status=status, limit=paging.limit, offset=paging.offset
    )
    return Page[TransferRead](items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{transfer_id}", response_model=TransferRead, responses=error_responses(401, 403, 404))
def get_transfer(transfer_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanRead) -> TransferRead:
    return transfer_service.get_transfer(db, transfer_id, user, ctx)


def _transition(db, transfer_id, action, user, ctx, reason=None) -> TransferRead:
    result = transfer_service.transition(db, transfer_id, action, user, ctx, reason=reason)
    db.commit()
    return result


@router.post("/{transfer_id}/approve", response_model=TransferRead, responses=_TRANSITION_ERRORS)
def approve_transfer(transfer_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanApprove) -> TransferRead:
    """**requested → approved** (source branch). Reserves the units at the source, all lines or none
    (409 `insufficient_stock`)."""
    return _transition(db, transfer_id, "approve", user, ctx)


@router.post("/{transfer_id}/reject", response_model=TransferRead, responses=_TRANSITION_ERRORS)
def reject_transfer(
    transfer_id: uuid.UUID, body: RejectRequest, db: DbSession, ctx: Ctx, user: CanApprove
) -> TransferRead:
    """**requested → rejected** (source branch). A reason is required. No stock effect."""
    return _transition(db, transfer_id, "reject", user, ctx, reason=body.reason)


@router.post("/{transfer_id}/dispatch", response_model=TransferRead, responses=_TRANSITION_ERRORS)
def dispatch_transfer(transfer_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanDispatch) -> TransferRead:
    """**approved → dispatched** (source branch). The reserved units leave the source and are in transit."""
    return _transition(db, transfer_id, "dispatch", user, ctx)


@router.post("/{transfer_id}/receive", response_model=TransferRead, responses=_TRANSITION_ERRORS)
def receive_transfer(transfer_id: uuid.UUID, db: DbSession, ctx: Ctx, user: CanReceive) -> TransferRead:
    """**dispatched → received** (destination branch). Exactly the dispatched units are added at the destination."""
    return _transition(db, transfer_id, "receive", user, ctx)


@router.post("/{transfer_id}/cancel", response_model=TransferRead, responses=_TRANSITION_ERRORS)
def cancel_transfer(
    transfer_id: uuid.UUID,
    db: DbSession,
    ctx: Ctx,
    user: CanCancel,
    body: Annotated[TransferCancelRequest | None, Body()] = None,
) -> TransferRead:
    """**requested / approved → cancelled** (either branch). Releases any reservation at the source.
    Not possible once dispatched: the goods are on the road."""
    return _transition(db, transfer_id, "cancel", user, ctx, reason=body.reason if body else None)
