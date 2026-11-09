"""Orders: lifecycle, state machine, stock effects, retries, rollback and branch isolation."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import AuditLog, AuditOutcome, IdempotencyKey, Order, OrderStatus, StockMovement
from tests.conftest import auth_headers, ledger_matches_inventory, stock_of

ORDERS = "/api/v1/orders"


@pytest.fixture
def shop(world, make_product, set_stock):
    """Kandy with 10 phones and 5 tablets."""
    phone, tablet = make_product("PHN-001", price="1000.00"), make_product("TAB-001", price="500.00")
    set_stock(world["kandy"], phone, 10)
    set_stock(world["kandy"], tablet, 5)
    return world | {"phone": phone, "tablet": tablet}


def new_order(client, shop, lines, *, user="kandy_staff", reserve=False, branch="kandy", key=None):
    headers = auth_headers(shop[user])
    if key:
        headers["Idempotency-Key"] = key
    payload = {
        "branch_id": str(shop[branch].id),
        "customer_name": "Saman",
        "reserve": reserve,
        "items": [{"product_id": str(shop[p].id), "quantity": q} for p, q in lines],
    }
    return client.post(ORDERS, json=payload, headers=headers)


def act(client, shop, order_id, action, user="kandy_staff", json=None):
    return client.post(f"{ORDERS}/{order_id}/{action}", json=json, headers=auth_headers(shop[user]))


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


# --- Happy path -------------------------------------------------------------


def test_full_lifecycle_and_stock_effects(client, db, shop):
    res = new_order(client, shop, [("phone", 3), ("tablet", 1)])
    assert res.status_code == 201, res.text
    order = res.json()
    assert order["status"] == "pending"
    assert order["total_amount"] == "3500.00"
    assert order["allowed_actions"] == ["cancel", "reserve"]
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)  # pending holds nothing

    assert act(client, shop, order["id"], "reserve").json()["status"] == "reserved"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 3)
    assert stock_of(db, shop["kandy"], shop["tablet"]) == (5, 1)

    assert act(client, shop, order["id"], "confirm").json()["status"] == "confirmed"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 3)

    fulfilled = act(client, shop, order["id"], "fulfill").json()
    assert fulfilled["status"] == "fulfilled" and fulfilled["allowed_actions"] == []
    assert fulfilled["fulfilled_at"] is not None
    assert stock_of(db, shop["kandy"], shop["phone"]) == (7, 0)
    assert stock_of(db, shop["kandy"], shop["tablet"]) == (4, 0)
    assert ledger_matches_inventory(db)


def test_create_and_reserve_in_one_call(client, db, shop):
    res = new_order(client, shop, [("phone", 4)], reserve=True)
    assert res.status_code == 201
    assert res.json()["status"] == "reserved"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 4)


def test_price_is_snapshotted(client, db, shop):
    order = new_order(client, shop, [("phone", 1)]).json()
    shop["phone"].unit_price = 9999
    db.commit()
    again = client.get(f"{ORDERS}/{order['id']}", headers=auth_headers(shop["kandy_staff"])).json()
    assert again["items"][0]["unit_price"] == "1000.00"


def test_transitions_are_audited(client, db, shop):
    order = new_order(client, shop, [("phone", 1)], reserve=True).json()
    act(client, shop, order["id"], "cancel", json={"reason": "Customer changed mind"})
    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == order["id"]).order_by(AuditLog.created_at)
    ).all()
    assert actions == ["order.create", "order.reserve", "order.cancel"]


# --- Rejected over-reservation and atomicity --------------------------------


def test_over_reservation_rejected_and_nothing_created(client, db, shop):
    res = new_order(client, shop, [("phone", 11)], reserve=True)
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"]["shortages"] == [{"product_id": str(shop["phone"].id), "requested": 11, "available": 10}]
    assert count(db, Order) == 0
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)


def test_failed_reservation_is_audited_even_though_request_rolled_back(client, db, shop):
    new_order(client, shop, [("phone", 11)], reserve=True)
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "order.reserve"))
    assert entry.outcome == AuditOutcome.FAILURE
    assert entry.details["reason"] == "insufficient_stock"


def test_multi_line_order_is_all_or_nothing(client, db, shop):
    """Phones are available, tablets are not: neither may be reserved."""
    order = new_order(client, shop, [("phone", 3), ("tablet", 6)]).json()
    res = act(client, shop, order["id"], "reserve")

    assert res.status_code == 409
    shortages = res.json()["error"]["details"]["shortages"]
    assert [s["product_id"] for s in shortages] == [str(shop["tablet"].id)]
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)
    assert stock_of(db, shop["kandy"], shop["tablet"]) == (5, 0)
    assert db.get(Order, uuid.UUID(order["id"])).status == OrderStatus.PENDING
    assert count(db, StockMovement, StockMovement.reference_type == "order") == 0


def test_crash_mid_transaction_leaves_no_partial_changes(db, shop, monkeypatch):
    """Simulate a failure (e.g. lost DB connection) AFTER the first line was reserved.
    The whole transaction must roll back: no reservation, no ledger row, order still pending."""
    from app.services import stock

    real_apply = stock.apply_movement
    calls = {"n": 0}

    def flaky_apply(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real_apply(*args, **kwargs)

    client = TestClient(app, raise_server_exceptions=False)
    order = new_order(client, shop, [("phone", 3), ("tablet", 2)]).json()
    monkeypatch.setattr(stock, "apply_movement", flaky_apply)

    res = act(client, shop, order["id"], "reserve")

    assert calls["n"] == 2  # the first line really was applied before the crash
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "internal_error"
    assert "simulated" not in res.text  # internals are not leaked
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)
    assert stock_of(db, shop["kandy"], shop["tablet"]) == (5, 0)
    assert db.get(Order, uuid.UUID(order["id"])).status == OrderStatus.PENDING
    assert count(db, StockMovement, StockMovement.reference_type == "order") == 0


# --- Cancellation -----------------------------------------------------------


def test_cancel_releases_exactly_what_was_reserved(client, db, shop):
    other = new_order(client, shop, [("phone", 2)], reserve=True).json()  # must stay untouched
    order = new_order(client, shop, [("phone", 3), ("tablet", 2)], reserve=True).json()
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 5)

    res = act(client, shop, order["id"], "cancel", json={"reason": "Customer changed mind"})

    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    assert res.json()["cancellation_reason"] == "Customer changed mind"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 2)  # only the other order's 2 remain
    assert stock_of(db, shop["kandy"], shop["tablet"]) == (5, 0)
    releases = db.scalars(
        select(StockMovement).where(
            StockMovement.reference_id == uuid.UUID(order["id"]), StockMovement.movement_type == "reservation_release"
        )
    ).all()
    assert sorted(m.reserved_delta for m in releases) == [-3, -2]
    assert db.get(Order, uuid.UUID(other["id"])).status == OrderStatus.RESERVED
    assert ledger_matches_inventory(db)


def test_cancel_confirmed_order_releases_stock(client, db, shop):
    order = new_order(client, shop, [("phone", 4)], reserve=True).json()
    act(client, shop, order["id"], "confirm")
    assert act(client, shop, order["id"], "cancel").status_code == 200
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)


def test_cancel_pending_order_touches_no_stock(client, db, shop):
    order = new_order(client, shop, [("phone", 4)]).json()
    assert act(client, shop, order["id"], "cancel").json()["status"] == "cancelled"
    assert count(db, StockMovement, StockMovement.reference_type == "order") == 0


def test_cancelling_twice_releases_only_once(client, db, shop):
    order = new_order(client, shop, [("phone", 4)], reserve=True).json()
    first = act(client, shop, order["id"], "cancel")
    second = act(client, shop, order["id"], "cancel")
    assert first.status_code == second.status_code == 200
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 0)
    assert count(db, StockMovement, StockMovement.movement_type == "reservation_release") == 1


# --- Invalid transitions ----------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "action"),
    [
        ([], "confirm"),  # pending -> confirmed skips reservation
        ([], "fulfill"),  # pending -> fulfilled
        (["reserve"], "fulfill"),  # reserved -> fulfilled skips confirmation
        (["reserve", "confirm", "fulfill"], "cancel"),  # fulfilled is final
        (["reserve", "confirm", "fulfill"], "reserve"),
        (["cancel"], "reserve"),  # cancelled is final
        (["cancel"], "confirm"),
    ],
)
def test_invalid_transition_rejected_without_changing_data(client, db, shop, setup, action):
    order = new_order(client, shop, [("phone", 2)]).json()
    for step in setup:
        assert act(client, shop, order["id"], step).status_code == 200
    before_status = db.get(Order, uuid.UUID(order["id"])).status
    before_stock = stock_of(db, shop["kandy"], shop["phone"])
    before_ledger = count(db, StockMovement)

    res = act(client, shop, order["id"], action)

    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "invalid_transition"
    assert error["details"]["current_status"] == before_status.value
    db.expire_all()
    assert db.get(Order, uuid.UUID(order["id"])).status == before_status
    assert stock_of(db, shop["kandy"], shop["phone"]) == before_stock
    assert count(db, StockMovement) == before_ledger


def test_unknown_order_404(client, shop):
    assert act(client, shop, uuid.uuid4(), "reserve").status_code == 404


# --- Retries and idempotency ------------------------------------------------


def test_repeated_reserve_does_not_double_reserve(client, db, shop):
    order = new_order(client, shop, [("phone", 4)]).json()
    for _ in range(3):
        res = act(client, shop, order["id"], "reserve")
        assert res.status_code == 200 and res.json()["status"] == "reserved"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 4)
    assert count(db, StockMovement, StockMovement.movement_type == "reservation") == 1


def test_repeated_confirm_is_harmless(client, db, shop):
    order = new_order(client, shop, [("phone", 4)], reserve=True).json()
    first, second = act(client, shop, order["id"], "confirm"), act(client, shop, order["id"], "confirm")
    assert first.json() == second.json()
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 4)


def test_retried_create_with_same_key_returns_same_order(client, db, shop):
    key = str(uuid.uuid4())
    first = new_order(client, shop, [("phone", 4)], reserve=True, key=key)
    retry = new_order(client, shop, [("phone", 4)], reserve=True, key=key)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert count(db, Order) == 1
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 4)


def test_same_key_with_different_body_is_rejected(client, db, shop):
    key = str(uuid.uuid4())
    new_order(client, shop, [("phone", 1)], key=key)
    res = new_order(client, shop, [("phone", 2)], key=key)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "idempotency_key_reused"
    assert count(db, Order) == 1


def test_keys_are_scoped_per_user(client, db, shop):
    key = str(uuid.uuid4())
    new_order(client, shop, [("phone", 1)], key=key, user="kandy_staff")
    new_order(client, shop, [("phone", 1)], key=key, user="kandy_manager")
    assert count(db, Order) == 2


def test_failed_request_does_not_burn_the_key(client, db, shop):
    """A rejected request isn't cached: after a restock the same retry succeeds."""
    key = str(uuid.uuid4())
    assert new_order(client, shop, [("phone", 12)], reserve=True, key=key).status_code == 409
    assert count(db, IdempotencyKey) == 0

    client.post(
        f"/api/v1/branches/{shop['kandy'].id}/inventory/adjustments",
        json={"product_id": str(shop["phone"].id), "quantity_delta": 5, "reason": "Restock"},
        headers=auth_headers(shop["kandy_manager"]),
    )
    assert new_order(client, shop, [("phone", 12)], reserve=True, key=key).status_code == 201


def test_retried_adjustment_applies_once(client, db, shop):
    url = f"/api/v1/branches/{shop['kandy'].id}/inventory/adjustments"
    headers = auth_headers(shop["kandy_manager"]) | {"Idempotency-Key": "grn-2026-0001"}
    body = {"product_id": str(shop["phone"].id), "quantity_delta": 5, "reason": "Goods received"}
    first, retry = client.post(url, json=body, headers=headers), client.post(url, json=body, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert stock_of(db, shop["kandy"], shop["phone"]) == (15, 0)


# --- Validation and permissions ---------------------------------------------


def test_order_validation(client, shop):
    headers = auth_headers(shop["kandy_staff"])
    pid = str(shop["phone"].id)
    bad = [
        {"branch_id": str(shop["kandy"].id), "customer_name": "A", "items": []},
        {"branch_id": str(shop["kandy"].id), "customer_name": "A", "items": [{"product_id": pid, "quantity": 0}]},
        {"branch_id": str(shop["kandy"].id), "customer_name": "", "items": [{"product_id": pid, "quantity": 1}]},
        {
            "branch_id": str(shop["kandy"].id),
            "customer_name": "A",
            "items": [{"product_id": pid, "quantity": 1}, {"product_id": pid, "quantity": 2}],
        },
    ]
    for payload in bad:
        assert client.post(ORDERS, json=payload, headers=headers).status_code == 422, payload


def test_cannot_order_inactive_product(client, db, shop):
    shop["phone"].is_active = False
    db.commit()
    res = new_order(client, shop, [("phone", 1)])
    assert res.status_code == 422 and res.json()["error"]["code"] == "product_inactive"


def test_cannot_order_unknown_product(client, shop):
    res = client.post(
        ORDERS,
        headers=auth_headers(shop["kandy_staff"]),
        json={
            "branch_id": str(shop["kandy"].id),
            "customer_name": "A",
            "items": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
        },
    )
    assert res.status_code == 422 and res.json()["error"]["code"] == "product_not_found"


def test_staff_cannot_create_order_for_other_branch(client, db, shop):
    res = new_order(client, shop, [("phone", 1)], branch="colombo")
    assert res.status_code == 403
    assert count(db, Order) == 0


def test_staff_cannot_touch_other_branch_order(client, db, shop):
    order = new_order(client, shop, [("phone", 2)], reserve=True).json()
    for action in ("confirm", "cancel"):
        res = act(client, shop, order["id"], action, user="colombo_staff")
        assert res.status_code == 403
    assert client.get(f"{ORDERS}/{order['id']}", headers=auth_headers(shop["colombo_staff"])).status_code == 403
    assert db.get(Order, uuid.UUID(order["id"])).status == OrderStatus.RESERVED
    assert stock_of(db, shop["kandy"], shop["phone"]) == (10, 2)


def test_order_list_is_branch_scoped(client, shop, make_product, set_stock):
    set_stock(shop["colombo"], shop["phone"], 5)
    new_order(client, shop, [("phone", 1)])
    new_order(client, shop, [("phone", 1)], user="colombo_staff", branch="colombo")

    kandy = client.get(ORDERS, headers=auth_headers(shop["kandy_staff"])).json()
    assert kandy["total"] == 1 and kandy["items"][0]["branch_id"] == str(shop["kandy"].id)
    assert client.get(ORDERS, headers=auth_headers(shop["admin"])).json()["total"] == 2
    filtered = client.get(ORDERS, params={"status": "reserved"}, headers=auth_headers(shop["admin"])).json()
    assert filtered["total"] == 0
