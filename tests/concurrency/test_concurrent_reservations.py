"""The concurrency challenge, and related races, run with REAL threads against PostgreSQL.

Scenario from the task:
    Branch A has 10 units. Three reservation requests for 6, 5 and 4 units
    arrive at the same moment. Total reserved must never exceed 10, and a
    request that fails must leave no partial reservation behind.

Why it is safe: every reservation runs
    BEGIN
      SELECT ... FROM inventory WHERE branch, product FOR UPDATE   -- one at a time
      check available >= requested
      UPDATE inventory SET reserved = reserved + n; INSERT ledger row
    COMMIT
so the three requests queue on the row lock and each one checks the
availability left by the one before it. A threading.Barrier releases all
threads at the same instant to make the race as tight as possible, and the
scenario is repeated many times.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.main import app
from app.models import Order, OrderStatus, StockMovement
from app.schemas.orders import LineIn, OrderCreate
from app.services import idempotency, order_service, stock
from app.services.audit import RequestContext
from tests.conftest import auth_headers, ledger_matches_inventory, stock_of

pytestmark = pytest.mark.concurrency

ROUNDS = 20

# Every combination the task accepts: whoever locks the row first wins; the rest
# get whatever is left. With 10 units at most two of {6, 5, 4} can fit.
VALID_OUTCOMES = [{6, 4}, {5, 4}, {6}, {5}, {4}]


def _order(branch, product, quantity, reserve=True):
    return OrderCreate(
        branch_id=branch.id,
        customer_name=f"Customer wanting {quantity}",
        items=[LineIn(product_id=product.id, quantity=quantity)],
        reserve=reserve,
    )


def _race(actor, branch, product, quantities):
    """Fire one create-and-reserve per quantity, all released at the same instant."""
    barrier = threading.Barrier(len(quantities))

    def attempt(quantity):
        with SessionLocal() as db:
            barrier.wait()
            try:
                order_service.create_order(db, _order(branch, product, quantity), actor, RequestContext())
                db.commit()
                return quantity, True
            except stock.InsufficientStockError:
                db.rollback()
                return quantity, False

    with ThreadPoolExecutor(max_workers=len(quantities)) as pool:
        return list(pool.map(attempt, quantities))


def test_two_concurrent_requests_competing_for_limited_stock(db, world, make_product, set_stock):
    """Required scenario: two requests (7 and 6) for 10 units at the same instant. Both can't
    fit, so exactly one must win and the other must fail cleanly. Repeated ROUNDS times."""
    winners = set()
    for round_no in range(ROUNDS):
        product = make_product(f"PAIR-{round_no}")
        set_stock(world["kandy"], product, 10)

        results = _race(world["kandy_staff"], world["kandy"], product, [7, 6])
        succeeded = [q for q, ok in results if ok]

        assert len(succeeded) == 1, f"round {round_no}: {results}"
        assert stock_of(db, world["kandy"], product) == (10, succeeded[0])
        winners.add(succeeded[0])
    assert ledger_matches_inventory(db)
    print(f"\nWinners observed over {ROUNDS} rounds: {sorted(winners)}")


def test_three_simultaneous_reservations_never_oversell(db, world, make_product, set_stock):
    """The task's scenario (10 units; 6, 5, 4 requested at once), repeated ROUNDS times."""
    seen_outcomes = set()
    for round_no in range(ROUNDS):
        product = make_product(f"RACE-{round_no}")
        set_stock(world["kandy"], product, 10)

        results = _race(world["kandy_staff"], world["kandy"], product, [6, 5, 4])
        succeeded = {q for q, ok in results if ok}
        seen_outcomes.add(frozenset(succeeded))

        on_hand, reserved = stock_of(db, world["kandy"], product)
        assert sum(succeeded) <= 10, f"round {round_no}: oversold {succeeded}"
        assert succeeded in VALID_OUTCOMES, f"round {round_no}: unexpected outcome {succeeded}"
        assert (on_hand, reserved) == (10, sum(succeeded)), f"round {round_no}"

        # Failed requests left nothing behind: no order rows, no ledger rows.
        orders = (
            db.scalars(select(Order).join(Order.items).where(Order.items.any(product_id=product.id))).unique().all()
        )
        assert len(orders) == len(succeeded)
        assert all(o.status == OrderStatus.RESERVED for o in orders)
        movements = db.scalars(
            select(StockMovement).where(
                StockMovement.product_id == product.id, StockMovement.movement_type == "reservation"
            )
        ).all()
        assert sorted(m.reserved_delta for m in movements) == sorted(succeeded)

    assert ledger_matches_inventory(db)
    print(f"\nOutcomes observed over {ROUNDS} rounds: {sorted(sorted(o) for o in seen_outcomes)}")


def test_many_buyers_for_the_last_units(db, world, make_product, set_stock):
    """25 customers each want 1 of 10 units: exactly 10 succeed, 15 are refused."""
    product = make_product("LAST-UNITS")
    set_stock(world["kandy"], product, 10)

    results = _race(world["kandy_staff"], world["kandy"], product, [1] * 25)

    assert sum(ok for _, ok in results) == 10
    assert stock_of(db, world["kandy"], product) == (10, 10)
    assert ledger_matches_inventory(db)


def test_second_transaction_waits_for_the_row_lock(db, world, make_product, set_stock):
    """Shows the mechanism directly: while one transaction holds the lock on the
    inventory row, another reservation for the same row blocks until it commits,
    and then sees the committed numbers (not the ones from before it waited)."""
    product = make_product("LOCKED")
    set_stock(world["kandy"], product, 10)
    actor = world["kandy_staff"]

    holder = SessionLocal()
    item = stock.lock_inventory(holder, world["kandy"].id, [product.id])[product.id]  # lock held from here

    outcome = {}

    def contender():
        with SessionLocal() as s:
            try:
                order_service.create_order(s, _order(world["kandy"], product, 5), actor, RequestContext())
                s.commit()
                outcome["result"] = "reserved"
            except stock.InsufficientStockError:
                s.rollback()
                outcome["result"] = "insufficient"
            outcome["finished_at"] = time.monotonic()

    thread = threading.Thread(target=contender)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive(), "the contender should be blocked on the row lock"

    # The lock holder reserves 8 units and commits, which leaves 2 available.
    stock.apply_movement(
        holder,
        item,
        movement_type="reservation",
        on_hand_delta=0,
        reserved_delta=8,
        reason="Held by first transaction",
        actor_id=actor.id,
        reference=stock.StockReference("manual"),
    )
    released_at = time.monotonic()
    holder.commit()
    holder.close()

    thread.join(timeout=10)
    assert outcome["finished_at"] >= released_at
    assert outcome["result"] == "insufficient"  # it re-checked against the committed 2 available
    assert stock_of(db, world["kandy"], product) == (10, 8)


def test_concurrent_duplicate_requests_with_one_idempotency_key(db, world, make_product, set_stock):
    """A client fires the same request 5 times at once (aggressive retry). Exactly one order is
    created and every copy gets the same answer."""
    product = make_product("DUP")
    set_stock(world["kandy"], product, 10)
    actor, key = world["kandy_staff"], f"retry-{uuid.uuid4()}"
    body = _order(world["kandy"], product, 3)
    barrier = threading.Barrier(5)

    def send():
        with SessionLocal() as s:
            barrier.wait()
            return idempotency.execute(
                s,
                user=actor,
                key=key,
                endpoint="POST /orders",
                payload=body,
                status_code=201,
                operation=lambda: order_service.create_order(s, body, actor, RequestContext()),
            )

    with ThreadPoolExecutor(5) as pool:
        outcomes = list(pool.map(lambda _: send(), range(5)))

    assert len({o.body["id"] for o in outcomes}) == 1
    assert sum(not o.replayed for o in outcomes) == 1
    assert db.scalar(select(func.count()).select_from(Order)) == 1
    assert stock_of(db, world["kandy"], product) == (10, 3)


def test_cancel_and_fulfil_racing_on_one_order(db, world, make_product, set_stock):
    """Cancel and fulfil arrive together for a confirmed order. The order row lock lets exactly one
    win; the stock must reflect that one and only that one."""
    for round_no in range(10):
        product = make_product(f"CF-{round_no}")
        set_stock(world["kandy"], product, 10)
        actor = world["kandy_manager"]
        with SessionLocal() as s:
            order = order_service.create_order(s, _order(world["kandy"], product, 4), actor, RequestContext())
            order_service.transition(s, order.id, "confirm", actor, RequestContext())
            s.commit()

        barrier = threading.Barrier(2)

        def run(action, barrier=barrier, order=order, actor=actor):
            with SessionLocal() as s:
                barrier.wait()
                try:
                    order_service.transition(s, order.id, action, actor, RequestContext())
                    s.commit()
                    return action
                except order_service.InvalidTransitionError:
                    s.rollback()
                    return None

        with ThreadPoolExecutor(2) as pool:
            winners = [w for w in pool.map(run, ["cancel", "fulfill"]) if w]

        assert len(winners) == 1, f"round {round_no}: {winners}"
        final = db.get(Order, order.id)
        db.refresh(final)
        if winners == ["cancel"]:
            assert final.status == OrderStatus.CANCELLED
            assert stock_of(db, world["kandy"], product) == (10, 0)
        else:
            assert final.status == OrderStatus.FULFILLED
            assert stock_of(db, world["kandy"], product) == (6, 0)
    assert ledger_matches_inventory(db)


def test_race_through_the_http_api(db, world, make_product, set_stock):
    """The same 6/5/4 race, but through the full HTTP stack (auth, validation, routing)."""
    product = make_product("HTTP-RACE")
    set_stock(world["kandy"], product, 10)
    headers = auth_headers(world["kandy_staff"])
    barrier = threading.Barrier(3)

    def post(quantity):
        client = TestClient(app)
        payload = {
            "branch_id": str(world["kandy"].id),
            "customer_name": f"Wants {quantity}",
            "reserve": True,
            "items": [{"product_id": str(product.id), "quantity": quantity}],
        }
        barrier.wait()
        return quantity, client.post("/api/v1/orders", json=payload, headers=headers)

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(post, [6, 5, 4]))

    succeeded = {q for q, r in results if r.status_code == 201}
    refused = [r for _, r in results if r.status_code != 201]
    assert succeeded in VALID_OUTCOMES
    assert all(r.status_code == 409 and r.json()["error"]["code"] == "insufficient_stock" for r in refused)
    assert stock_of(db, world["kandy"], product) == (10, sum(succeeded))


def test_orders_and_transfer_approvals_compete_for_the_same_stock(db, world, make_product, set_stock):
    """Orders and a transfer approval race for the same 10 units. Whatever the
    interleaving, total reserved never exceeds 10 and the ledger stays consistent."""
    from app.schemas.transfers import TransferCreate
    from app.services import transfer_service

    for round_no in range(10):
        product = make_product(f"MIX-{round_no}")
        set_stock(world["kandy"], product, 10)
        manager = world["kandy_manager"]
        with SessionLocal() as s:
            transfer = transfer_service.create_transfer(
                s,
                TransferCreate(
                    source_branch_id=world["kandy"].id,
                    destination_branch_id=world["colombo"].id,
                    items=[LineIn(product_id=product.id, quantity=6)],
                ),
                manager,
                RequestContext(),
            )
            s.commit()

        barrier = threading.Barrier(3)

        def approve(barrier=barrier, transfer=transfer, manager=manager):
            with SessionLocal() as s:
                barrier.wait()
                try:
                    transfer_service.transition(s, transfer.id, "approve", manager, RequestContext())
                    s.commit()
                    return 6
                except stock.InsufficientStockError:
                    s.rollback()
                    return 0

        def buy(quantity, barrier=barrier, product=product, manager=manager):
            with SessionLocal() as s:
                barrier.wait()
                try:
                    order_service.create_order(s, _order(world["kandy"], product, quantity), manager, RequestContext())
                    s.commit()
                    return quantity
                except stock.InsufficientStockError:
                    s.rollback()
                    return 0

        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(approve), pool.submit(buy, 5), pool.submit(buy, 4)]
            reserved_total = sum(f.result() for f in futures)

        assert reserved_total <= 10, f"round {round_no}"
        assert stock_of(db, world["kandy"], product) == (10, reserved_total), f"round {round_no}"
    assert ledger_matches_inventory(db)
