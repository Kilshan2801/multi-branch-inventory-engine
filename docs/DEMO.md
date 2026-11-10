# Demo run sheet

The task asks to demonstrate **a successful order, a rejected over-reservation, a cancellation and a
transfer**, and to explain the concurrency safeguards. This sheet does it in about 10 minutes.

## Before the demo

```bash
# fresh data (optional): drop and recreate the schema, then seed
alembic downgrade base && alembic upgrade head
python -m scripts.seed

uvicorn app.main:app            # terminal 1: leave running
```

Open <http://localhost:8000/docs> in a browser, and a second terminal for the scripts.

## Option A: scripted (fast and reliable)

```bash
python -m scripts.demo          # runs all four scenarios and prints the stock after every step
```

| # | What it shows | What to point out |
|---|---|---|
| 1 | Successful order: create + reserve → confirm → fulfil | `reserved` rises at reservation; `on_hand` only drops at fulfilment |
| 2 | Rejected over-reservation (999 phones) | `409 insufficient_stock` lists the shortage; stock and orders unchanged |
| 3 | Cancellation | Exactly the 3 reserved units are released; cancelling again changes nothing |
| 4 | Transfer Kandy → Colombo | Colombo can't approve its own request (403); reserved at approve, leaves Kandy at dispatch, arrives at receive; the total never changes |
| + | Branch boundary | Colombo staff reading Kandy's stock → 403 (and audited) |

Then the concurrency challenge:

```bash
python -m scripts.concurrency_demo --rounds 3
```

Point out that the winning combination changes between rounds ({6,4} or {5,4}), but reserved never exceeds 10.

## Option B: in Swagger (shows the API itself)

1. **Log in** as `staff.kdy@example.com` → *Authorize* with the token.
2. `GET /api/v1/branches` → copy Kandy's `id`. `GET /api/v1/products` → copy `PHN-001`'s `id`.
3. `GET /branches/{kandy}/inventory`: phones `on_hand 10, reserved 0, available 10`.
4. **Successful order:** `POST /orders` with `"reserve": true`, 2 phones → `201`, `status: reserved`.
   Then `/confirm`, then `/fulfill`. Check inventory: `on_hand 8`.
5. **Rejected over-reservation:** `POST /orders`, 999 phones → `409 insufficient_stock`. Inventory unchanged.
6. **Invalid transition:** `/fulfill` on a *reserved* order → `409 invalid_transition` with `allowed_actions`.
7. **Cancellation:** create a reserved order for 3 → inventory shows `reserved 3` → `/cancel` → `reserved 0`.
8. **Retry safety:** send the same `POST /orders` twice with header `Idempotency-Key: demo-1` → same order id, second response has `Idempotent-Replayed: true`.
9. **Transfer:** log in as `manager.cmb@example.com`, `POST /transfers` (Kandy → Colombo, 3 tablets).
   Log in as `manager.kdy@example.com`: `/approve`, `/dispatch`. Log in as `staff.cmb@example.com`: `/receive`.
   Check both branches' inventory after each step.
10. **Audit:** log in as admin, `GET /audit-logs?action=order` to see every step, including failures.
11. **Ledger:** `GET /branches/{kandy}/stock-movements` to see every change with reason, actor and reference.

## Points to be ready to explain

| Topic | Where to look |
|---|---|
| Schema, constraints, why `available` is generated | [SCHEMA.md](SCHEMA.md), `app/models/` |
| Role permissions and branch scope | [ROLES.md](ROLES.md), `app/core/permissions.py`, `app/services/authorization.py` |
| Order and transfer state machines | `TRANSITIONS` in `app/services/order_service.py` and `transfer_service.py` |
| Concurrency safeguards and why row locking | [CONCURRENCY.md](CONCURRENCY.md), `app/services/stock.py` |
| Transaction rollback | services never commit; `test_crash_mid_transaction_leaves_no_partial_changes` |
| Duplicate requests | `app/services/idempotency.py` |
| Test coverage | README → *The test scenarios the task asked for*; `pytest -v` |
| Trade-offs and limitations | [REPORT.md](REPORT.md) sections 3 and 7 |
