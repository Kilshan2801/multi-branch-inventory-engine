from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError
from app.core.security import create_access_token, verify_password
from app.models import AuditOutcome, User
from app.services.audit import RequestContext, record, record_now

# One message for every failure, so a caller can't learn which emails exist
# or which accounts are disabled.
_INVALID = "Invalid email or password"


def login(db: Session, email: str, password: str, ctx: RequestContext) -> tuple[str, int, User]:
    user = db.scalar(select(User).where(User.email == email.lower()))
    password_ok = verify_password(password, user.password_hash if user else None)

    failure_reason = None
    if user is None:
        failure_reason = "unknown_email"
    elif not password_ok:
        failure_reason = "wrong_password"
    elif not user.is_active:
        failure_reason = "user_inactive"
    elif user.branch is not None and not user.branch.is_active:
        failure_reason = "branch_inactive"

    if failure_reason:
        record_now(
            action="auth.login",
            outcome=AuditOutcome.FAILURE,
            ctx=ctx,
            actor_id=user.id if user else None,
            entity_type="user",
            entity_id=user.id if user else None,
            details={"reason": failure_reason, "email": email.lower()},
        )
        raise AuthenticationError(_INVALID, code="invalid_credentials")

    assert user is not None
    token, expires_in = create_access_token(user.id, user.role.value, user.branch_id)
    record(
        db,
        action="auth.login",
        ctx=ctx,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        branch_id=user.branch_id,
    )
    db.commit()
    return token, expires_in, user
