import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, AuditOutcome, User
from app.services.audit import RequestContext
from app.services.authorization import branch_scope, ensure_branch_access


def list_audit_logs(
    db: Session,
    actor: User,
    ctx: RequestContext,
    *,
    action: str | None,
    outcome: AuditOutcome | None,
    actor_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: str | None,
    branch_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[AuditLog], int]:
    """Admins see everything; branch managers only entries tied to their branch."""
    scope = branch_scope(actor)
    if scope is not None:
        if branch_id is not None:
            ensure_branch_access(actor, branch_id, ctx, action="audit.read")
        branch_id = scope

    query = select(AuditLog)
    if branch_id is not None:
        query = query.where(AuditLog.branch_id == branch_id)
    if action:
        # "order" matches order.create, order.cancel, ...
        query = query.where((AuditLog.action == action) | AuditLog.action.like(f"{action}.%"))
    if outcome is not None:
        query = query.where(AuditLog.outcome == outcome)
    if actor_id is not None:
        query = query.where(AuditLog.actor_id == actor_id)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if since is not None:
        query = query.where(AuditLog.created_at >= since)
    if until is not None:
        query = query.where(AuditLog.created_at < until)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), total
