from fastapi import APIRouter

from app.api.deps import Ctx, CurrentUser, DbSession
from app.core.permissions import permissions_for
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.common import error_responses
from app.schemas.users import MeResponse, UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse, responses=error_responses(401, 422))
def login(body: LoginRequest, db: DbSession, ctx: Ctx) -> TokenResponse:
    """Exchange email + password for a JWT access token."""
    token, expires_in, user = auth_service.login(db, body.email, body.password, ctx)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserRead.model_validate(user))


@router.get("/me", response_model=MeResponse, responses=error_responses(401))
def me(user: CurrentUser) -> MeResponse:
    """The authenticated user, plus the permissions their role grants."""
    return MeResponse(**UserRead.model_validate(user).model_dump(), permissions=permissions_for(user.role))
