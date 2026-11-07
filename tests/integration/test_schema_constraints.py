"""The database itself refuses invalid data, independent of application code.

These are the safety net under the service layer: even a bug in the API
cannot oversell, create negative stock, or rewrite ledger history.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import (
    AuditLog,
    AuditOutcome,
    InventoryItem,
    MovementType,
    Product,
    Role,
    StockMovement,
    Transfer,
    User,
)


@pytest.fixture
def product(db):
    p = Product(sku="SKU-A", name="Product A", unit_price=100)
    db.add(p)
    db.commit()
    return p


def _stock(db, branch, product, on_hand, reserved=0):
    item = InventoryItem(branch_id=branch.id, product_id=product.id, on_hand=on_hand, reserved=reserved)
    db.add(item)
    db.commit()
    return item


def test_available_is_computed_by_database(db, world, product):
    item = _stock(db, world["kandy"], product, on_hand=10, reserved=4)
    db.refresh(item)
    assert item.available == 6


def test_cannot_reserve_more_than_on_hand(db, world, product):
    with pytest.raises(IntegrityError, match="reserved_within_on_hand"):
        _stock(db, world["kandy"], product, on_hand=10, reserved=11)


def test_on_hand_cannot_go_negative(db, world, product):
    item = _stock(db, world["kandy"], product, on_hand=5)
    item.on_hand = -1
    with pytest.raises(IntegrityError, match="on_hand_non_negative"):
        db.commit()


def test_one_inventory_row_per_branch_and_product(db, world, product):
    _stock(db, world["kandy"], product, on_hand=1)
    with pytest.raises(IntegrityError, match="uq_inventory_branch_product"):
        _stock(db, world["kandy"], product, on_hand=2)


def _movement(world, product):
    return StockMovement(
        branch_id=world["kandy"].id,
        product_id=product.id,
        movement_type=MovementType.ADJUSTMENT,
        on_hand_delta=10,
        reserved_delta=0,
        on_hand_after=10,
        reserved_after=0,
        reason="Opening stock",
        reference_type="manual",
        actor_id=world["admin"].id,
    )


def test_stock_ledger_rejects_update(db, world, product):
    movement = _movement(world, product)
    db.add(movement)
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE stock_movements SET on_hand_delta = 999"))


def test_stock_ledger_rejects_delete(db, world, product):
    db.add(_movement(world, product))
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM stock_movements"))


def test_ledger_entry_needs_a_reason(db, world, product):
    movement = _movement(world, product)
    movement.reason = "   "
    db.add(movement)
    with pytest.raises(IntegrityError, match="reason_not_blank"):
        db.commit()


def test_audit_log_is_append_only(db, world):
    db.add(AuditLog(action="test", outcome=AuditOutcome.SUCCESS, actor_id=world["admin"].id))
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_logs SET action = 'tampered'"))


def test_transfer_needs_two_different_branches(db, world):
    db.add(
        Transfer(
            source_branch_id=world["kandy"].id,
            destination_branch_id=world["kandy"].id,
            requested_by=world["admin"].id,
        )
    )
    with pytest.raises(IntegrityError, match="distinct_branches"):
        db.commit()


def test_staff_without_branch_rejected_by_database(db):
    db.add(User(email="x@example.com", full_name="X", password_hash="x", role=Role.STAFF))
    with pytest.raises(IntegrityError, match="branch_matches_role"):
        db.commit()


def test_unknown_role_rejected_by_database(db):
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash, role) "
                "VALUES (gen_random_uuid(), 'y@example.com', 'Y', 'x', 'superuser')"
            )
        )
