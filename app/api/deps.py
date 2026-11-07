"""FastAPI dependencies: database session, request context, authentication, permissions."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.permissions import Permission, has_permission
from app.core.security import decode_access_token
from app.models import AuditOutcome, User
from app.services.audit import RequestContext, record_now

_bearer = HTTPBearer(auto_error=False, description="Paste the access_token from POST /api/v1/auth/login")

DbSession = Annotated[Session, Depends(get_db)]


def get_request_context(request: Request) -> RequestContext:
    return RequestContext(
        ip_address=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


Ctx = Annotated[RequestContext, Depends(get_request_context)]


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token", code="missing_token")

    user_id = decode_access_token(credentials.credentials)
    user = db.get(User, user_id)
    # Re-checked on every request, so deactivation is effective immediately.
    if user is None or not user.is_active or (user.branch is not None and not user.branch.is_active):
        raise AuthenticationError("Invalid access token", code="invalid_token")

    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(*permissions: Permission) -> Callable[..., User]:
    """Dependency factory: `user: User = Depends(require_permission(Permission.X))`."""

    def checker(user: CurrentUser, ctx: Ctx) -> User:
        missing = [p for p in permissions if not has_permission(user.role, p)]
        if missing:
            record_now(
                action="access.denied",
                outcome=AuditOutcome.DENIED,
                ctx=ctx,
                actor_id=user.id,
                branch_id=user.branch_id,
                details={
                    "reason": "missing_permission",
                    "required": [p.value for p in missing],
                    "role": user.role.value,
                },
            )
            raise PermissionDeniedError(
                "Your role does not allow this action", details={"required": [p.value for p in missing]}
            )
        return user

    return checker
