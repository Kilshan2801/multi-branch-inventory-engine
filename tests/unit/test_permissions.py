import pytest

from app.core.permissions import ROLE_PERMISSIONS, Permission, has_permission, permissions_for
from app.models import Role


def test_admin_has_every_permission():
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)


def test_every_role_is_defined():
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_branch_manager_can_do_everything_staff_can():
    assert ROLE_PERMISSIONS[Role.STAFF] < ROLE_PERMISSIONS[Role.BRANCH_MANAGER]


@pytest.mark.parametrize(
    "permission",
    [
        Permission.INVENTORY_ADJUST,
        Permission.USER_MANAGE,
        Permission.USER_READ,
        Permission.BRANCH_MANAGE,
        Permission.PRODUCT_MANAGE,
        Permission.TRANSFER_APPROVE,
        Permission.TRANSFER_DISPATCH,
        Permission.AUDIT_READ,
    ],
)
def test_staff_cannot(permission):
    assert not has_permission(Role.STAFF, permission)


@pytest.mark.parametrize(
    "permission",
    [
        Permission.ORDER_CREATE,
        Permission.ORDER_CONFIRM,
        Permission.ORDER_FULFILL,
        Permission.INVENTORY_READ,
        Permission.TRANSFER_RECEIVE,
    ],
)
def test_staff_can(permission):
    assert has_permission(Role.STAFF, permission)


@pytest.mark.parametrize("permission", [Permission.USER_MANAGE, Permission.BRANCH_MANAGE, Permission.PRODUCT_MANAGE])
def test_only_admin_manages_reference_data(permission):
    assert has_permission(Role.ADMIN, permission)
    assert not has_permission(Role.BRANCH_MANAGER, permission)


def test_permissions_for_is_sorted_strings():
    perms = permissions_for(Role.STAFF)
    assert perms == sorted(perms)
    assert all(isinstance(p, str) for p in perms)
