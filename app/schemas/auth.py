from pydantic import BaseModel, Field

from app.schemas.users import Email, UserRead


class LoginRequest(BaseModel):
    email: Email
    # Deliberately loose: a login must not reveal password rules, only succeed or fail.
    password: str = Field(min_length=1, max_length=128)

    model_config = {"json_schema_extra": {"examples": [{"email": "admin@example.com", "password": "your-password"}]}}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Lifetime of the token in seconds")
    user: UserRead
