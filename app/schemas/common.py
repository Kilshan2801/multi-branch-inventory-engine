from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str = Field(examples=["permission_denied"])
    message: str = Field(examples=["Your role does not allow this action"])
    details: Any = None


class ErrorResponse(BaseModel):
    """Shape of every non-2xx response."""

    error: ErrorBody
    request_id: str | None = None


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Document the standard error payload on a route, for OpenAPI."""
    return {code: {"model": ErrorResponse} for code in codes}
