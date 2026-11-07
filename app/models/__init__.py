"""Importing this package registers every table on Base.metadata (needed by Alembic)."""

from app.models.base import Base
from app.models.enums import AuditOutcome, MovementType, OrderStatus, Role, TransferStatus
from app.models.inventory import InventoryItem, StockMovement
from app.models.orders import Order, OrderItem, Transfer, TransferItem
from app.models.organization import Branch, Product, User
from app.models.system import AuditLog, IdempotencyKey

__all__ = [
    "AuditLog",
    "AuditOutcome",
    "Base",
    "Branch",
    "IdempotencyKey",
    "InventoryItem",
    "MovementType",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Product",
    "Role",
    "StockMovement",
    "Transfer",
    "TransferItem",
    "TransferStatus",
    "User",
]
