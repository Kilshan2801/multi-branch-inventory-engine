"""Transfers: lifecycle, conservation of stock, over-dispatch protection, permissions."""

import uuid

import pytest
from sqlalchemy import func, select, text

from app.models import AuditLog, Transfer, TransferStatus
from tests.conftest import auth_headers, ledger_matches_inventory, stock_of

TRANSFERS = "/api/v1/transfers"


@pytest.fixture
def net(world, make_product, set_stock):
    """Kandy has 10 phones and 5 tablets; Colombo has 2 phones and has never stocked tablets."""
    phone, tablet = make_product("PHN-001"), make_product("TAB-001")
    set_stock(world["kandy"], phone, 10)
    set_stock(world["kandy"], tablet, 5)
    set_stock(world["colombo"], phone, 2)
    return world | {"phone": phone, "tablet": tablet}


def request(client, net, lines, *, user="colombo_manager", source="kandy", dest="colombo", key=None):
    headers = auth_headers(net[user]) | ({"Idempotency-Key": key} if key else {})
    return client.post(
        TRANSFERS,
        headers=headers,
        json={
            "source_branch_id": str(net[source].id),
            "destination_branch_id": str(net[dest].id),
            "items": [{"product_id": str(net[p].id), "quantity": q} for p, q in lines],
            "notes": "Weekend sale",
        },
    )


def act(client, net, transfer_id, action, user, json=None):
    return client.post(f"{TRANSFERS}/{transfer_id}/{action}", json=json, headers=auth_headers(net[user]))


def total_units(db, product) -> int:
    """All units of a product in the business: on hand at every branch plus in transit."""
    on_hand = db.execute(
        text("SELECT COALESCE(SUM(on_hand), 0) FROM inventory WHERE product_id = :p"), {"p": product.id}
    ).scalar()
    in_transit = db.execute(
        text("""
        SELECT COALESCE(SUM(ti.quantity), 0) FROM transfer_items ti JOIN transfers t ON t.id = ti.transfer_id
        WHERE ti.product_id = :p AND t.status = 'dispatched'"""),
        {"p": product.id},
    ).scalar()
    return on_hand + in_transit


# --- Full lifecycle ---------------------------------------------------------


def test_full_transfer_lifecycle_conserves_stock(client, db, net):
    phone, tablet = net["phone"], net["tablet"]
    start_phones, start_tablets = total_units(db, phone), total_units(db, tablet)

    t = request(client, net, [("phone", 3), ("tablet", 2)])
    assert t.status_code == 201, t.text
    t = t.json()
    assert t["status"] == "requested" and t["allowed_actions"] == ["approve", "cancel", "reject"]
    assert stock_of(db, net["kandy"], phone) == (10, 0)

    approved = act(client, net, t["id"], "approve", "kandy_manager")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == str(net["kandy_manager"].id)
    assert stock_of(db, net["kandy"], phone) == (10, 3)  # reserved, still on the shelf

    dispatched = act(client, net, t["id"], "dispatch", "kandy_manager")
    assert dispatched.json()["status"] == "dispatched"
    assert stock_of(db, net["kandy"], phone) == (7, 0)  # left the building
    assert stock_of(db, net["colombo"], phone) == (2, 0)  # not arrived yet
    assert total_units(db, phone) == start_phones  # ... but counted as in transit

    received = act(client, net, t["id"], "receive", "colombo_staff")
    assert received.json()["status"] == "received" and received.json()["allowed_actions"] == []
    assert stock_of(db, net["colombo"], phone) == (5, 0)
    assert stock_of(db, net["colombo"], tablet) == (2, 0)  # inventory row created on arrival
    assert stock_of(db, net["kandy"], tablet) == (3, 0)

    assert total_units(db, phone) == start_phones
    assert total_units(db, tablet) == start_tablets
    assert ledger_matches_inventory(db)


def test_transfer_actions_are_audited(client, db, net):
    t = request(client, net, [("phone", 1)]).json()
    for action, user in [("approve", "kandy_manager"), ("dispatch", "kandy_manager"), ("receive", "colombo_staff")]:
        act(client, net, t["id"], action, user)
    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == t["id"]).order_by(AuditLog.created_at)
    ).all()
    assert actions == ["transfer.request", "transfer.approve", "transfer.dispatch", "transfer.receive"]


# --- Over-dispatch protection -----------------------------------------------


def test_transfer_cannot_dispatch_more_than_available(client, db, net):
    t = request(client, net, [("phone", 11)]).json()  # Kandy only has 10

    res = act(client, net, t["id"], "approve", "kandy_manager")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "insufficient_stock"
    assert res.json()["error"]["details"]["shortages"][0]["available"] == 10

    # Dispatch is impossible because it was never approved, and nothing moved.
    res = act(client, net, t["id"], "dispatch", "kandy_manager")
    assert res.status_code == 409 and res.json()["error"]["code"] == "invalid_transition"
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 0)
    assert db.get(Transfer, uuid.UUID(t["id"])).status == TransferStatus.REQUESTED


def test_stock_promised_to_orders_cannot_be_transferred(client, db, net):
    """8 phones are reserved for customers, so only 2 can be approved for transfer."""
    client.post(
        "/api/v1/orders",
        headers=auth_headers(net["kandy_staff"]),
        json={
            "branch_id": str(net["kandy"].id),
            "customer_name": "A",
            "reserve": True,
            "items": [{"product_id": str(net["phone"].id), "quantity": 8}],
        },
    )
    t = request(client, net, [("phone", 3)]).json()
    assert act(client, net, t["id"], "approve", "kandy_manager").status_code == 409
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 8)


def test_approved_transfer_stock_is_protected_until_dispatch(client, db, net):
    """Once approved, those units can't be sold or written off, so dispatch can't fail."""
    t = request(client, net, [("phone", 7)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")

    order = client.post(
        "/api/v1/orders",
        headers=auth_headers(net["kandy_staff"]),
        json={
            "branch_id": str(net["kandy"].id),
            "customer_name": "A",
            "reserve": True,
            "items": [{"product_id": str(net["phone"].id), "quantity": 4}],
        },
    )
    assert order.status_code == 409

    write_off = client.post(
        f"/api/v1/branches/{net['kandy'].id}/inventory/adjustments",
        headers=auth_headers(net["kandy_manager"]),
        json={"product_id": str(net["phone"].id), "quantity_delta": -4, "reason": "Damaged"},
    )
    assert write_off.status_code == 409

    assert act(client, net, t["id"], "dispatch", "kandy_manager").status_code == 200
    assert stock_of(db, net["kandy"], net["phone"]) == (3, 0)


def test_multi_line_approval_is_all_or_nothing(client, db, net):
    t = request(client, net, [("phone", 3), ("tablet", 6)]).json()  # tablets short
    assert act(client, net, t["id"], "approve", "kandy_manager").status_code == 409
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 0)
    assert stock_of(db, net["kandy"], net["tablet"]) == (5, 0)


# --- Reject and cancel ------------------------------------------------------


def test_reject_requires_reason_and_moves_nothing(client, db, net):
    t = request(client, net, [("phone", 3)]).json()
    assert act(client, net, t["id"], "reject", "kandy_manager", json={}).status_code == 422
    res = act(client, net, t["id"], "reject", "kandy_manager", json={"reason": "We need them ourselves"})
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"
    assert res.json()["status_reason"] == "We need them ourselves"
    assert res.json()["closed_by"] == str(net["kandy_manager"].id)
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 0)


def test_cancel_approved_transfer_releases_reservation(client, db, net):
    t = request(client, net, [("phone", 4), ("tablet", 1)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")
    res = act(client, net, t["id"], "cancel", "colombo_manager", json={"reason": "No longer needed"})
    assert res.status_code == 200 and res.json()["status"] == "cancelled"
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 0)
    assert stock_of(db, net["kandy"], net["tablet"]) == (5, 0)
    assert ledger_matches_inventory(db)


def test_cannot_cancel_after_dispatch(client, db, net):
    t = request(client, net, [("phone", 4)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")
    act(client, net, t["id"], "dispatch", "kandy_manager")
    res = act(client, net, t["id"], "cancel", "kandy_manager")
    assert res.status_code == 409
    assert res.json()["error"]["details"]["allowed_actions"] == ["receive"]
    assert stock_of(db, net["kandy"], net["phone"]) == (6, 0)


@pytest.mark.parametrize(
    ("setup", "action", "user"),
    [
        ([], "dispatch", "kandy_manager"),  # must be approved first
        ([], "receive", "colombo_staff"),  # nothing was sent
        (["approve"], "receive", "colombo_staff"),  # approved but not dispatched
        (["approve"], "reject", "kandy_manager"),  # too late to reject
        (["reject"], "approve", "kandy_manager"),  # rejected is final
        (["approve", "dispatch", "receive"], "cancel", "kandy_manager"),
    ],
)
def test_invalid_transfer_transitions(client, db, net, setup, action, user):
    t = request(client, net, [("phone", 2)]).json()
    for step in setup:
        actor = "colombo_staff" if step == "receive" else "kandy_manager"
        body = {"reason": "Not possible"} if step == "reject" else None
        assert act(client, net, t["id"], step, actor, json=body).status_code == 200
    before = (stock_of(db, net["kandy"], net["phone"]), stock_of(db, net["colombo"], net["phone"]))
    body = {"reason": "Not possible"} if action == "reject" else None

    res = act(client, net, t["id"], action, user, json=body)

    assert res.status_code == 409 and res.json()["error"]["code"] == "invalid_transition"
    assert (stock_of(db, net["kandy"], net["phone"]), stock_of(db, net["colombo"], net["phone"])) == before


# --- Retries ----------------------------------------------------------------


def test_repeated_dispatch_and_receive_apply_once(client, db, net):
    t = request(client, net, [("phone", 3)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")
    act(client, net, t["id"], "approve", "kandy_manager")
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 3)
    for _ in range(2):
        assert act(client, net, t["id"], "dispatch", "kandy_manager").status_code == 200
    for _ in range(2):
        assert act(client, net, t["id"], "receive", "colombo_staff").status_code == 200
    assert stock_of(db, net["kandy"], net["phone"]) == (7, 0)
    assert stock_of(db, net["colombo"], net["phone"]) == (5, 0)


def test_retried_transfer_request_with_same_key(client, db, net):
    first = request(client, net, [("phone", 1)], key="tr-001")
    retry = request(client, net, [("phone", 1)], key="tr-001")
    assert retry.json()["id"] == first.json()["id"]
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert db.scalar(select(func.count()).select_from(Transfer)) == 1


# --- Permissions ------------------------------------------------------------


def test_staff_cannot_request_transfer(client, net):
    assert request(client, net, [("phone", 1)], user="colombo_staff").status_code == 403


def test_destination_cannot_approve_its_own_request(client, db, net):
    t = request(client, net, [("phone", 3)]).json()
    res = act(client, net, t["id"], "approve", "colombo_manager")
    assert res.status_code == 403
    assert res.json()["error"]["message"] == "Only the source branch can do this"
    assert stock_of(db, net["kandy"], net["phone"]) == (10, 0)


def test_source_cannot_mark_as_received(client, db, net):
    t = request(client, net, [("phone", 3)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")
    act(client, net, t["id"], "dispatch", "kandy_manager")
    assert act(client, net, t["id"], "receive", "kandy_manager").status_code == 403
    assert stock_of(db, net["colombo"], net["phone"]) == (2, 0)


def test_unrelated_branch_cannot_see_or_touch_transfer(client, net, make_branch, make_user):
    from app.models import Role

    galle_manager = make_user(Role.BRANCH_MANAGER, make_branch("GLE"))
    t = request(client, net, [("phone", 1)]).json()
    net = net | {"galle_manager": galle_manager}
    assert client.get(f"{TRANSFERS}/{t['id']}", headers=auth_headers(galle_manager)).status_code == 403
    assert act(client, net, t["id"], "cancel", "galle_manager").status_code == 403
    listed = client.get(TRANSFERS, headers=auth_headers(galle_manager)).json()
    assert listed["total"] == 0


def test_cannot_request_between_two_foreign_branches(client, net, make_branch):
    galle = make_branch("GLE")
    net = net | {"galle": galle}
    res = request(client, net, [("phone", 1)], user="colombo_manager", source="kandy", dest="galle")
    assert res.status_code == 403


def test_both_branches_see_the_transfer(client, net):
    request(client, net, [("phone", 1)])
    for user in ("kandy_staff", "colombo_staff"):
        assert client.get(TRANSFERS, headers=auth_headers(net[user])).json()["total"] == 1


def test_in_transit_filter(client, net):
    t = request(client, net, [("phone", 1)]).json()
    act(client, net, t["id"], "approve", "kandy_manager")
    act(client, net, t["id"], "dispatch", "kandy_manager")
    res = client.get(TRANSFERS, params={"status": "dispatched"}, headers=auth_headers(net["admin"])).json()
    assert [x["id"] for x in res["items"]] == [t["id"]]


# --- Validation -------------------------------------------------------------


def test_same_source_and_destination_rejected(client, net):
    res = request(client, net, [("phone", 1)], source="kandy", dest="kandy")
    assert res.status_code == 422


def test_cannot_transfer_to_inactive_branch(client, net, make_branch):
    closed = make_branch("OLD", is_active=False)
    res = request(client, net | {"closed": closed}, [("phone", 1)], user="admin", dest="closed")
    assert res.status_code == 422 and res.json()["error"]["code"] == "branch_inactive"
