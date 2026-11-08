import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.enums import AuditOutcome, MovementType
from app.schemas.common import ORMModel

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class InventoryRead(BaseModel):
    branch_id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    product_name: str
    on_hand: int = Field(description="Units physically in the branch")
    reserved: int = Field(description="Units promised to orders or approved transfers")
    available: int = Field(description="on_hand - reserved: what can still be promised")
    updated_at: datetime


class AdjustmentCreate(BaseModel):
    product_id: uuid.UUID
    quantity_delta: int = Field(
        description="Positive to add stock (goods received, count correction), negative to remove (damage, loss)."
    )
    reason: Reason = Field(description="Mandatory: why the stock changed. Stored in the ledger.")

    @field_validator("quantity_delta")
    @classmethod
    def not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("quantity_delta must not be zero")
        if abs(value) > 1_000_000:
            raise ValueError("quantity_delta is unreasonably large")
        return value

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "product_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "quantity_delta": 10,
                    "reason": "Goods received, supplier invoice INV-1042",
                }
            ]
        }
    }


class StockMovementRead(ORMModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    product_id: uuid.UUID
    movement_type: MovementType
    on_hand_delta: int
    reserved_delta: int
    on_hand_after: int
    reserved_after: int
    reason: str
    reference_type: str | None
    reference_id: uuid.UUID | None
    actor_id: uuid.UUID
    created_at: datetime


class AdjustmentResult(BaseModel):
    inventory: InventoryRead
    movement: StockMovementRead


class AuditLogRead(ORMModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    outcome: AuditOutcome
    entity_type: str | None
    entity_id: str | None
    branch_id: uuid.UUID | None
    ip_address: str | None
    request_id: str | None
    details: dict[str, Any] | None
    created_at: datetime
