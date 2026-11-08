"""Request/response models for branches and products."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.common import ORMModel

BranchCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$"),
]
Sku = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"),
]
Text120 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text255 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


# --- Branches ---------------------------------------------------------------


class BranchCreate(BaseModel):
    code: BranchCode = Field(description="Short unique code, e.g. KDY. Stored upper-case.")
    name: Text120
    address: Text255 | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [{"code": "NGB", "name": "Negombo Branch", "address": "Main Street, Negombo"}]
        }
    }


class BranchUpdate(BaseModel):
    """Partial update. Send `is_active: false` to deactivate (soft delete)."""

    name: Text120 | None = None
    address: Text255 | None = None
    is_active: bool | None = None


class BranchRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    address: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# --- Products ---------------------------------------------------------------


class ProductCreate(BaseModel):
    sku: Sku = Field(description="Unique stock-keeping unit. Stored upper-case.")
    name: Text200
    description: str | None = Field(default=None, max_length=2000)
    unit_price: Price

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"sku": "PHN-001", "name": "Smartphone X", "description": "128 GB, black", "unit_price": "89999.00"}
            ]
        }
    }


class ProductUpdate(BaseModel):
    """Partial update. Send `is_active: false` to deactivate (soft delete). The SKU never changes."""

    name: Text200 | None = None
    description: str | None = Field(default=None, max_length=2000)
    unit_price: Price | None = None
    is_active: bool | None = None


class ProductRead(ORMModel):
    id: uuid.UUID
    sku: str
    name: str
    description: str | None
    unit_price: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime
