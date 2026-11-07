import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints, model_validator

from app.core.security import BCRYPT_MAX_BYTES
from app.models.enums import Role
from app.schemas.common import ORMModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def _lower(value: str) -> str:
    return value.lower()


Email = Annotated[EmailStr, AfterValidator(_lower)]


def _check_password(value: str) -> str:
    if len(value.encode()) > BCRYPT_MAX_BYTES:
        raise ValueError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes")
    if value.strip() != value:
        raise ValueError("Password must not start or end with whitespace")
    if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
        raise ValueError("Password must contain at least one letter and one digit")
    return value


Password = Annotated[str, Field(min_length=8, max_length=72), AfterValidator(_check_password)]


def _check_role_branch(role: Role | None, branch_id: uuid.UUID | None) -> None:
    if role == Role.ADMIN and branch_id is not None:
        raise ValueError("Admins are global and must not have a branch_id")
    if role in (Role.BRANCH_MANAGER, Role.STAFF) and branch_id is None:
        raise ValueError("Branch managers and staff must have a branch_id")


class UserCreate(BaseModel):
    email: Email
    full_name: Name
    password: Password
    role: Role
    branch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def role_matches_branch(self) -> Self:
        _check_role_branch(self.role, self.branch_id)
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "email": "staff.kandy2@example.com",
                    "full_name": "Nimal Perera",
                    "password": "Str0ngPass!",
                    "role": "staff",
                    "branch_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                }
            ]
        }
    }


class UserUpdate(BaseModel):
    """Partial update. Role and branch_id, if either is sent, are validated together."""

    full_name: Name | None = None
    role: Role | None = None
    branch_id: uuid.UUID | None = None
    is_active: bool | None = None
    password: Password | None = None


class UserRead(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    branch_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MeResponse(UserRead):
    permissions: list[str]
