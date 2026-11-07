"""Branch-scope checks (the second half of RBAC; see app/core/permissions.py)."""

import uuid

from app.core.errors import PermissionDeniedError
from app.models import AuditOutcome, Role, User
from app.services.audit import RequestContext, record_now


def can_access_branch(user: User, branch_id: uuid.UUID | None) -> bool:
    if user.role == Role.ADMIN:
        return True
    return branch_id is not None and user.branch_id == branch_id


def ensure_branch_access(user: User, branch_id: uuid.UUID | None, ctx: RequestContext, *, action: str) -> None:
    """Raise 403 (and audit the attempt) unless `user` may act on `branch_id`."""
    if can_access_branch(user, branch_id):
        return
    record_now(
        action="access.denied",
        outcome=AuditOutcome.DENIED,
        ctx=ctx,
        actor_id=user.id,
        branch_id=branch_id,
        details={"attempted": action, "reason": "branch_scope", "user_branch_id": str(user.branch_id)},
    )
    raise PermissionDeniedError("You do not have access to this branch", code="branch_access_denied")


def branch_scope(user: User) -> uuid.UUID | None:
    """Branch a user's list queries must be restricted to; None means all branches (admin)."""
    return None if user.role == Role.ADMIN else user.branch_id
