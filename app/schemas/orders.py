import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.models.enums import OrderStatus

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class LineIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=10_000)


def _unique_products(items: list[LineIn]) -> None:
    seen = set()
    for item in items:
        if item.product_id in seen:
            raise ValueError(f"Product {item.product_id} appears more than once; combine the quantities")
        seen.add(item.product_id)


class OrderCreate(BaseModel):
    branch_id: uuid.UUID
    customer_name: Name
    customer_contact: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    items: list[LineIn] = Field(min_length=1, max_length=50)
    reserve: bool = Field(
        default=False,
        description="Also reserve stock in the same transaction. If any line is short, "
        "nothing is created (409 insufficient_stock).",
    )

    @model_validator(mode="after")
    def check_items(self) -> Self:
        _unique_products(self.items)
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "branch_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "customer_name": "Saman Kumara",
                    "customer_contact": "+94 77 123 4567",
                    "items": [{"product_id": "8b7e1c2a-4d3f-4a5b-9c6d-7e8f9a0b1c2d", "quantity": 2}],
                    "reserve": True,
                }
            ]
        }
    }


class CancelRequest(BaseModel):
    reason: OptionalReason | None = None


class OrderItemRead(BaseModel):
    product_id: uuid.UUID
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class OrderRead(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    status: OrderStatus
    customer_name: str
    customer_contact: str | None
    notes: str | None
    items: list[OrderItemRead]
    total_amount: Decimal
    allowed_actions: list[str] = Field(description="Transitions possible from the current status")
    created_by: uuid.UUID
    cancellation_reason: str | None
    created_at: datetime
    updated_at: datetime
    reserved_at: datetime | None
    confirmed_at: datetime | None
    fulfilled_at: datetime | None
    cancelled_at: datetime | None
