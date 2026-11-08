"""Stock levels, manual adjustments, the ledger and branch isolation."""

import uuid

from sqlalchemy import func, select

from app.models import AuditLog, MovementType, StockMovement
from tests.conftest import auth_headers, ledger_matches_inventory, stock_of


def url(branch, suffix=""):
    return f"/api/v1/branches/{branch.id}{suffix}"


def adjust(client, user, branch, product, delta, reason="Goods received"):
    return client.post(
        url(branch, "/inventory/adjustments"),
        json={"product_id": str(product.id), "quantity_delta": delta, "reason": reason},
        headers=auth_headers(user),
    )


def test_manager_adds_stock_and_ledger_records_it(client, db, world, make_product):
    product = make_product("PHN-001")
    res = adjust(client, world["kandy_manager"], world["kandy"], product, 10, "Supplier delivery INV-1")

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["inventory"]["on_hand"] == 10
    assert body["inventory"]["available"] == 10
    movement = body["movement"]
    assert movement["movement_type"] == "adjustment"
    assert movement["on_hand_delta"] == 10 and movement["on_hand_after"] == 10
    assert movement["reason"] == "Supplier delivery INV-1"
    assert movement["actor_id"] == str(world["kandy_manager"].id)
    assert movement["reference_type"] == "manual"
    assert ledger_matches_inventory(db)


def test_adjustment_is_audited(client, db, world, make_product):
    product = make_product()
    adjust(client, world["kandy_manager"], world["kandy"], product, 5)
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "inventory.adjust"))
    assert entry.actor_id == world["kandy_manager"].id
    assert entry.branch_id == world["kandy"].id
    assert entry.details["quantity_delta"] == 5


def test_remove_stock(client, db, world, make_product, set_stock):
    product = make_product()
    set_stock(world["kandy"], product, 10)
    res = adjust(client, world["kandy_manager"], world["kandy"], product, -3, "Damaged in storage")
    assert res.status_code == 201
    assert stock_of(db, world["kandy"], product) == (7, 0)
    assert ledger_matches_inventory(db)


def test_cannot_remove_more_than_on_hand(client, db, world, make_product, set_stock):
    product = make_product()
    set_stock(world["kandy"], product, 5)
    res = adjust(client, world["kandy_manager"], world["kandy"], product, -6, "Count correction")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "insufficient_stock"
    assert stock_of(db, world["kandy"], product) == (5, 0)


def test_cannot_remove_reserved_units(client, db, world, make_product, set_stock):
    product = make_product()
    item = set_stock(world["kandy"], product, 10)
    item.reserved = 8  # simulate units promised to orders
    db.commit()

    res = adjust(client, world["kandy_manager"], world["kandy"], product, -3, "Count correction")
    assert res.status_code == 409
    assert "only 2 are not reserved" in res.json()["error"]["message"]
    assert stock_of(db, world["kandy"], product) == (10, 8)


def test_removing_from_never_stocked_product_is_409(client, world, make_product):
    res = adjust(client, world["kandy_manager"], world["kandy"], make_product(), -1, "Count correction")
    assert res.status_code == 409


def test_staff_cannot_adjust_stock(client, db, world, make_product):
    product = make_product()
    res = adjust(client, world["kandy_staff"], world["kandy"], product, 10)
    assert res.status_code == 403
    assert stock_of(db, world["kandy"], product) == (0, 0)


def test_manager_cannot_adjust_other_branch(client, db, world, make_product):
    product = make_product()
    res = adjust(client, world["kandy_manager"], world["colombo"], product, 10)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "branch_access_denied"
    assert stock_of(db, world["colombo"], product) == (0, 0)


def test_reason_is_mandatory_and_zero_rejected(client, world, make_product):
    product = make_product()
    headers = auth_headers(world["kandy_manager"])
    res = client.post(
        url(world["kandy"], "/inventory/adjustments"),
        json={"product_id": str(product.id), "quantity_delta": 0, "reason": " "},
        headers=headers,
    )
    assert res.status_code == 422
    fields = {d["field"] for d in res.json()["error"]["details"]}
    assert {"body.quantity_delta", "body.reason"} <= fields


def test_cannot_stock_inactive_product(client, world, make_product):
    res = adjust(client, world["kandy_manager"], world["kandy"], make_product(is_active=False), 5)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "product_inactive"


def test_inactive_product_stock_can_still_be_written_off(client, db, world, make_product, set_stock):
    product = make_product()
    set_stock(world["kandy"], product, 5)
    product.is_active = False
    db.commit()
    assert adjust(client, world["kandy_manager"], world["kandy"], product, -5, "Discontinued").status_code == 201


def test_cannot_adjust_inactive_branch(client, world, make_branch, make_product):
    closed = make_branch("OLD", is_active=False)
    res = adjust(client, world["admin"], closed, make_product(), 5)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "branch_inactive"


def test_unknown_product_404(client, world):
    res = client.post(
        url(world["kandy"], "/inventory/adjustments"),
        json={"product_id": str(uuid.uuid4()), "quantity_delta": 1, "reason": "Test"},
        headers=auth_headers(world["kandy_manager"]),
    )
    assert res.status_code == 404


# --- Reading ----------------------------------------------------------------


def test_staff_reads_own_branch_inventory(client, world, make_product, set_stock):
    phone, tablet = make_product("PHN-001"), make_product("TAB-001")
    set_stock(world["kandy"], phone, 10)
    set_stock(world["kandy"], tablet, 3)
    set_stock(world["colombo"], phone, 99)

    res = client.get(url(world["kandy"], "/inventory"), headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 200
    rows = {r["sku"]: r for r in res.json()["items"]}
    assert set(rows) == {"PHN-001", "TAB-001"}
    assert rows["PHN-001"]["on_hand"] == 10 and rows["PHN-001"]["available"] == 10


def test_staff_cannot_read_other_branch_inventory(client, db, world):
    res = client.get(url(world["colombo"], "/inventory"), headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 403
    denied = db.scalar(select(AuditLog).where(AuditLog.action == "access.denied"))
    assert denied.details["attempted"] == "inventory.read"


def test_single_inventory_row(client, world, make_product, set_stock):
    product = make_product("PHN-001")
    set_stock(world["kandy"], product, 4)
    res = client.get(url(world["kandy"], f"/inventory/{product.id}"), headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 200 and res.json()["available"] == 4


def test_never_stocked_product_is_404_inventory_not_found(client, world, make_product):
    product = make_product()
    res = client.get(url(world["kandy"], f"/inventory/{product.id}"), headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "inventory_not_found"


def test_ledger_is_listed_newest_first(client, world, make_product):
    product = make_product()
    manager = world["kandy_manager"]
    adjust(client, manager, world["kandy"], product, 10, "First")
    adjust(client, manager, world["kandy"], product, -2, "Second")

    res = client.get(
        url(world["kandy"], "/stock-movements"),
        params={"product_id": str(product.id)},
        headers=auth_headers(world["kandy_staff"]),
    )
    assert res.status_code == 200
    assert [m["reason"] for m in res.json()["items"]] == ["Second", "First"]
    assert res.json()["items"][0]["on_hand_after"] == 8


def test_ledger_rows_have_distinct_timestamps_within_one_transaction(db, world, make_product):
    """clock_timestamp() (migration 0002) keeps rows written in one transaction ordered."""
    from app.services.stock import StockReference, apply_movement, lock_inventory

    product = make_product()
    item = lock_inventory(db, world["kandy"].id, [product.id], create_missing=True)[product.id]
    for _ in range(3):
        apply_movement(
            db,
            item,
            movement_type=MovementType.ADJUSTMENT,
            on_hand_delta=1,
            reserved_delta=0,
            reason="tick",
            actor_id=world["admin"].id,
            reference=StockReference("manual"),
        )
        db.flush()
    db.commit()
    distinct = db.scalar(select(func.count(func.distinct(StockMovement.created_at))))
    assert distinct == 3


# --- Audit log --------------------------------------------------------------


def test_manager_sees_only_own_branch_audit(client, world, make_product):
    product = make_product()
    adjust(client, world["kandy_manager"], world["kandy"], product, 1)
    adjust(client, world["colombo_manager"], world["colombo"], product, 1)

    res = client.get("/api/v1/audit-logs", params={"action": "inventory"}, headers=auth_headers(world["kandy_manager"]))
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["branch_id"] == str(world["kandy"].id)


def test_staff_cannot_read_audit(client, world):
    assert client.get("/api/v1/audit-logs", headers=auth_headers(world["kandy_staff"])).status_code == 403


def test_admin_filters_audit_by_action_prefix(client, world, make_product):
    adjust(client, world["admin"], world["kandy"], make_product(), 1)
    res = client.get("/api/v1/audit-logs", params={"action": "inventory"}, headers=auth_headers(world["admin"]))
    assert {e["action"] for e in res.json()["items"]} == {"inventory.adjust"}
