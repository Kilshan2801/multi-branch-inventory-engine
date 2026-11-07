"""Audit trail.

Two ways to write an entry, chosen by what must happen if the request fails:

* record()      joins the caller's transaction. Use it for successful changes:
                the audit row commits together with the change, or not at all.
* record_now()  commits immediately on its own connection. Use it for failed
                logins and denied access: the request is about to be rejected
                (and rolled back) but the attempt itself must still be logged.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import AuditLog, AuditOutcome

logger = logging.getLogger("app.audit")


@dataclass(frozen=True)
class RequestContext:
    ip_address: str | None = None
    request_id: str | None = None


def _build(
    action: str,
    outcome: AuditOutcome,
    ctx: RequestContext,
    actor_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: Any,
    branch_id: uuid.UUID | None,
    details: dict[str, Any] | None,
) -> AuditLog:
    logger.info(
        "audit action=%s outcome=%s actor=%s entity=%s:%s branch=%s request_id=%s",
        action,
        outcome.value,
        actor_id,
        entity_type,
        entity_id,
        branch_id,
        ctx.request_id,
    )
    return AuditLog(
        action=action,
        outcome=outcome,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        branch_id=branch_id,
        ip_address=ctx.ip_address,
        request_id=ctx.request_id,
        details=details,
    )


def record(
    db: Session,
    *,
    action: str,
    ctx: RequestContext,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    branch_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(_build(action, outcome, ctx, actor_id, entity_type, entity_id, branch_id, details))


def record_now(
    *,
    action: str,
    ctx: RequestContext,
    outcome: AuditOutcome,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    branch_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        with SessionLocal() as db, db.begin():
            db.add(_build(action, outcome, ctx, actor_id, entity_type, entity_id, branch_id, details))
    except Exception:  # auditing must never turn a 401/403 into a 500
        logger.exception("Failed to write audit entry for action=%s", action)
