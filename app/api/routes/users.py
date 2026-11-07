import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Ctx, DbSession, require_permission
from app.core.permissions import Permission
from app.models import Role, User
from app.schemas.common import Page, error_responses
from app.schemas.users import UserCreate, UserRead, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["Users"])

CanReadUsers = Annotated[User, Depends(require_permission(Permission.USER_READ))]
CanManageUsers = Annotated[User, Depends(require_permission(Permission.USER_MANAGE))]


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 409, 422),
)
def create_user(body: UserCreate, db: DbSession, ctx: Ctx, actor: CanManageUsers) -> User:
    """Create a user (admin only)."""
    return user_service.create_user(db, body, actor, ctx)


@router.get("", response_model=Page[UserRead], responses=error_responses(401, 403))
def list_users(
    db: DbSession,
    ctx: Ctx,
    actor: CanReadUsers,
    role: Role | None = None,
    branch_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserRead]:
    """List users. Branch managers only ever see users of their own branch."""
    users, total = user_service.list_users(
        db, actor, role=role, branch_id=branch_id, is_active=is_active, limit=limit, offset=offset, ctx=ctx
    )
    return Page[UserRead](items=[UserRead.model_validate(u) for u in users], total=total, limit=limit, offset=offset)


@router.get("/{user_id}", response_model=UserRead, responses=error_responses(401, 403, 404))
def get_user(user_id: uuid.UUID, db: DbSession, ctx: Ctx, actor: CanReadUsers) -> User:
    return user_service.get_user(db, user_id, actor, ctx)


@router.patch("/{user_id}", response_model=UserRead, responses=error_responses(401, 403, 404, 409, 422))
def update_user(user_id: uuid.UUID, body: UserUpdate, db: DbSession, ctx: Ctx, actor: CanManageUsers) -> User:
    """Update name, role, branch, password, or deactivate (`is_active: false`). Admin only.
    Users are never deleted: their id is referenced by the stock ledger and audit trail."""
    return user_service.update_user(db, user_id, body, actor, ctx)
