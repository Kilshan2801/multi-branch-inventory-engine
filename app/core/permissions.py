"""Role-based access control.

Two independent checks protect every operation:

1. Permission: does this ROLE allow this kind of action at all?
   (e.g. staff may not adjust stock). Defined once, in ROLE_PERMISSIONS.
2. Branch scope: is this USER allowed to act on THIS branch?
   Admins act on any branch; managers and staff only on their own.

docs/ROLES.md is the human-readable version of this file; keep them in sync.
"""

from enum import StrEnum

from app.models.enums import Role


class Permission(StrEnum):
    # Organisation
    BRANCH_READ = "branch:read"
    BRANCH_MANAGE = "branch:manage"
    PRODUCT_READ = "product:read"
    PRODUCT_MANAGE = "product:manage"
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    # Inventory
    INVENTORY_READ = "inventory:read"
    INVENTORY_ADJUST = "inventory:adjust"
    # Orders
    ORDER_READ = "order:read"
    ORDER_CREATE = "order:create"
    ORDER_CONFIRM = "order:confirm"
    ORDER_FULFILL = "order:fulfill"
    ORDER_CANCEL = "order:cancel"
    # Transfers
    TRANSFER_READ = "transfer:read"
    TRANSFER_REQUEST = "transfer:request"
    TRANSFER_APPROVE = "transfer:approve"  # approve or reject (source branch)
    TRANSFER_DISPATCH = "transfer:dispatch"  # source branch
    TRANSFER_RECEIVE = "transfer:receive"  # destination branch
    TRANSFER_CANCEL = "transfer:cancel"
    # Oversight
    AUDIT_READ = "audit:read"


_STAFF = frozenset(
    {
        Permission.BRANCH_READ,
        Permission.PRODUCT_READ,
        Permission.INVENTORY_READ,
        Permission.ORDER_READ,
        Permission.ORDER_CREATE,
        Permission.ORDER_CONFIRM,
        Permission.ORDER_FULFILL,
        Permission.ORDER_CANCEL,
        Permission.TRANSFER_READ,
        Permission.TRANSFER_RECEIVE,
    }
)

_BRANCH_MANAGER = _STAFF | frozenset(
    {
        Permission.USER_READ,
        Permission.INVENTORY_ADJUST,
        Permission.TRANSFER_REQUEST,
        Permission.TRANSFER_APPROVE,
        Permission.TRANSFER_DISPATCH,
        Permission.TRANSFER_CANCEL,
        Permission.AUDIT_READ,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.BRANCH_MANAGER: _BRANCH_MANAGER,
    Role.STAFF: _STAFF,
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def permissions_for(role: Role) -> list[str]:
    return sorted(p.value for p in ROLE_PERMISSIONS.get(role, frozenset()))
