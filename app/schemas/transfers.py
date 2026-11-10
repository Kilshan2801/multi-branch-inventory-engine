import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.models.enums import TransferStatus
from app.schemas.orders import LineIn, _unique_products

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class TransferCreate(BaseModel):
    source_branch_id: uuid.UUID = Field(description="Branch the stock leaves")
    destination_branch_id: uuid.UUID = Field(description="Branch the stock arrives at")
    items: list[LineIn] = Field(min_length=1, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.source_branch_id == self.destination_branch_id:
            raise ValueError("Source and destination branch must be different")
        _unique_products(self.items)
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "source_branch_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "destination_branch_id": "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
                    "items": [{"product_id": "8b7e1c2a-4d3f-4a5b-9c6d-7e8f9a0b1c2d", "quantity": 3}],
                    "notes": "Colombo is out of stock for the weekend sale",
                }
            ]
        }
    }


class RejectRequest(BaseModel):
    reason: Reason


class TransferCancelRequest(BaseModel):
    reason: Reason | None = None


class TransferItemRead(BaseModel):
    product_id: uuid.UUID
    quantity: int


class TransferRead(BaseModel):
    id: uuid.UUID
    source_branch_id: uuid.UUID
    destination_branch_id: uuid.UUID
    status: TransferStatus
    items: list[TransferItemRead]
    notes: str | None
    status_reason: str | None = Field(description="Why it was rejected or cancelled")
    allowed_actions: list[str]
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    dispatched_by: uuid.UUID | None
    received_by: uuid.UUID | None
    closed_by: uuid.UUID | None = Field(description="Who rejected or cancelled it")
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    dispatched_at: datetime | None
    received_at: datetime | None
    closed_at: datetime | None
