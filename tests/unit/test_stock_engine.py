"""Pure-logic checks of the stock engine (no database)."""

import uuid

import pytest

from app.models import InventoryItem, MovementType
from app.services.stock import InsufficientStockError, StockReference, apply_movement, ensure_available


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def item(on_hand, reserved=0):
    return InventoryItem(branch_id=uuid.uuid4(), product_id=uuid.uuid4(), on_hand=on_hand, reserved=reserved)


def test_ensure_available_lists_every_shortage():
    a, b, c = item(10, 4), item(2), item(5)
    items = {a.product_id: a, b.product_id: b, c.product_id: c}
    missing = uuid.uuid4()
    with pytest.raises(InsufficientStockError) as exc:
        ensure_available(items, {a.product_id: 7, b.product_id: 2, c.product_id: 9, missing: 1}, branch_id=uuid.uuid4())
    shortages = {s["product_id"]: s for s in exc.value.details["shortages"]}
    assert set(shortages) == {str(a.product_id), str(c.product_id), str(missing)}
    assert shortages[str(a.product_id)]["available"] == 6
    assert shortages[str(missing)]["available"] == 0


def test_ensure_available_passes_when_exactly_enough():
    a = item(10, 4)
    ensure_available({a.product_id: a}, {a.product_id: 6}, branch_id=uuid.uuid4())


@pytest.mark.parametrize(
    ("on_hand", "reserved", "d_on_hand", "d_reserved"),
    [
        (5, 0, -6, 0),  # on_hand below zero
        (5, 0, 0, 6),  # reserve more than on hand
        (5, 2, 0, -3),  # release more than reserved
        (5, 4, -2, 0),  # remove reserved units
    ],
)
def test_apply_movement_refuses_invalid_results(on_hand, reserved, d_on_hand, d_reserved):
    row = item(on_hand, reserved)
    with pytest.raises(InsufficientStockError):
        apply_movement(
            FakeSession(),
            row,
            movement_type=MovementType.ADJUSTMENT,
            on_hand_delta=d_on_hand,
            reserved_delta=d_reserved,
            reason="x",
            actor_id=uuid.uuid4(),
            reference=StockReference("manual"),
        )
    assert (row.on_hand, row.reserved) == (on_hand, reserved)


def test_apply_movement_updates_row_and_writes_ledger_entry():
    session, row = FakeSession(), item(10, 2)
    order_id = uuid.uuid4()
    movement = apply_movement(
        session,
        row,
        movement_type=MovementType.RESERVATION,
        on_hand_delta=0,
        reserved_delta=3,
        reason="Order",
        actor_id=uuid.uuid4(),
        reference=StockReference("order", order_id),
    )
    assert (row.on_hand, row.reserved) == (10, 5)
    assert session.added == [movement]
    assert (movement.on_hand_after, movement.reserved_after) == (10, 5)
    assert movement.reference_id == order_id
