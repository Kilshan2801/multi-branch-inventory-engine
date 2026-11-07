import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessValidationError, ConflictError, NotFoundError
from app.core.security import hash_password
from app.models import Branch, Role, User
from app.schemas.users import UserCreate, UserUpdate, _check_role_branch
from app.services.audit import RequestContext, record
from app.services.authorization import branch_scope, ensure_branch_access


def _require_active_branch(db: Session, branch_id: uuid.UUID | None) -> None:
    if branch_id is None:
        return
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessValidationError(
            "Branch does not exist", code="branch_not_found", details={"branch_id": str(branch_id)}
        )
    if not branch.is_active:
        raise BusinessValidationError(
            "Branch is inactive", code="branch_inactive", details={"branch_id": str(branch_id)}
        )


def create_user(db: Session, data: UserCreate, actor: User, ctx: RequestContext) -> User:
    _require_active_branch(db, data.branch_id)
    user = User(
        email=data.email,
        full_name=data.full_name,
        password_hash=hash_password(data.password),
        role=data.role,
        branch_id=data.branch_id,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("A user with this email already exists", code="email_taken") from exc

    record(
        db,
        action="user.create",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="user",
        entity_id=user.id,
        branch_id=user.branch_id,
        details={"email": user.email, "role": user.role.value},
    )
    db.commit()
    return user


def list_users(
    db: Session,
    actor: User,
    *,
    role: Role | None,
    branch_id: uuid.UUID | None,
    is_active: bool | None,
    limit: int,
    offset: int,
    ctx: RequestContext,
) -> tuple[list[User], int]:
    scope = branch_scope(actor)
    if scope is not None:
        if branch_id is not None:
            ensure_branch_access(actor, branch_id, ctx, action="user.list")
        branch_id = scope

    query = select(User)
    if role is not None:
        query = query.where(User.role == role)
    if branch_id is not None:
        query = query.where(User.branch_id == branch_id)
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    users = db.scalars(query.order_by(User.created_at, User.id).limit(limit).offset(offset)).all()
    return list(users), total


def get_user(db: Session, user_id: uuid.UUID, actor: User, ctx: RequestContext) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if actor.role != Role.ADMIN and user.id != actor.id:
        ensure_branch_access(actor, user.branch_id, ctx, action="user.read")
    return user


def update_user(db: Session, user_id: uuid.UUID, data: UserUpdate, actor: User, ctx: RequestContext) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise BusinessValidationError("No fields to update", code="empty_update")

    if user.id == actor.id and (changes.get("is_active") is False or changes.get("role", Role.ADMIN) != Role.ADMIN):
        raise ConflictError("You cannot deactivate or demote your own account", code="self_lockout")

    new_role = changes.get("role", user.role)
    # Promoting to admin clears the branch; otherwise keep the current branch unless a new one is sent.
    new_branch = (
        None if new_role == Role.ADMIN and "branch_id" not in changes else changes.get("branch_id", user.branch_id)
    )
    try:
        _check_role_branch(new_role, new_branch)
    except ValueError as exc:
        raise BusinessValidationError(str(exc), code="invalid_role_branch") from exc
    if new_branch != user.branch_id:
        _require_active_branch(db, new_branch)

    audit_changes = {k: v for k, v in changes.items() if k != "password"}
    if "full_name" in changes:
        user.full_name = changes["full_name"]
    if "is_active" in changes:
        user.is_active = changes["is_active"]
    if "password" in changes:
        user.password_hash = hash_password(changes["password"])
        audit_changes["password"] = "changed"
    user.role = new_role
    user.branch_id = new_branch

    record(
        db,
        action="user.update",
        ctx=ctx,
        actor_id=actor.id,
        entity_type="user",
        entity_id=user.id,
        branch_id=user.branch_id,
        details={"changes": {k: str(v) for k, v in audit_changes.items()}},
    )
    db.commit()
    db.refresh(user)
    return user
