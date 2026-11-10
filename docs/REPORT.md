# Technical report

**Multi-Branch Inventory Reservation & Order Processing Engine**
Kilshan Perinpanayagam · Neirah Tech Solution software engineering assessment

## 1. Summary

A FastAPI + PostgreSQL backend that manages stock across branches, customer orders and inter-branch
transfers. The design goal was that **inventory numbers can never become wrong**: not under concurrent
requests, not when a client retries, not when an operation fails half-way, and not when someone tries to
act outside their branch. Each of those threats has a specific defence, and each defence has an automated
test that runs against a real PostgreSQL database (188 tests, about 20 seconds).

## 2. Architecture

```
            HTTP (JSON, JWT bearer)
                   │
┌──────────────────▼───────────────────┐
│ api/routes   thin: validate input,   │  Pydantic schemas validate every request
│              check role permission,  │  require_permission() checks the role
│              call a service, respond │
├──────────────────────────────────────┤
│ services     business rules          │  order / transfer state machines,
│              branch-scope checks     │  idempotency, audit,
│              transaction boundaries  │  stock.py: the ONLY code that changes stock
├──────────────────────────────────────┤
│ models       tables + constraints    │  SQLAlchemy 2.0
├──────────────────────────────────────┤
│ PostgreSQL   CHECK / FK / UNIQUE,    │  last line of defence
│              row locks, triggers     │
└──────────────────────────────────────┘
```

- **Routes never touch the database directly**, and **services never commit**. The route commits once
  at the end of a successful request, and any exception rolls the whole request back. One request = one transaction.
- **One stock engine** (`app/services/stock.py`) is used by adjustments, orders and transfers. It locks
  rows, checks availability for every line before changing any, applies the change and writes the ledger
  entry. A new feature can't forget the rules because it can't change stock any other way.
- **Integrity is enforced in the database as well as in Python.** `reserved ≤ on_hand`, non-negative
  quantities, one inventory row per (branch, product), distinct transfer branches, and the admin ⇔ no-branch
  rule are constraints, so even a bug can't store an impossible value. `available` is a generated column,
  and triggers make the stock ledger and audit log append-only.

Schema and ER diagram: [SCHEMA.md](SCHEMA.md). Endpoints: [API.md](API.md).

## 3. Key decisions

Full list with trade-offs: [DECISIONS.md](DECISIONS.md). The most important:

| Decision | Why | Trade-off |
|---|---|---|
| **Pessimistic row locking** (`SELECT … FOR UPDATE`) for every stock change | Concurrent requests for the same stock queue and each sees the previous one's result; multi-line orders are checked as a whole. Verified by removing the lock: the race test then oversold 15 of 10. | A very hot product is processed one request at a time (each takes milliseconds). |
| **Fixed lock order** (header row, then inventory sorted by product id) | Makes deadlocks between our own transactions impossible. | None in practice. |
| **Idempotency keys** on creates and adjustments, stored in the same transaction as the work | A retry returns the original response; concurrent duplicates wait on a unique index and replay. | Keys are kept forever (would need expiry in production). |
| **State transitions are idempotent** (repeating one returns 200 unchanged) | A client that timed out can safely retry. | A late retry after the order moved on gets a 409 instead of 200. |
| **Releases and receipts computed from the ledger** | Cancelling releases exactly what was reserved; a transfer delivers exactly what was dispatched. | One extra aggregate query per transition. |
| **Transfer approval reserves source stock** | Approved units can't be sold or written off, so dispatch can never exceed what is there. | Stock is held from approval, not just from dispatch. |
| **Stock is "in transit" between dispatch and receipt** | Models reality; `Σ on_hand + Σ in transit` stays constant and is tested. | In-transit units are visible only via dispatched transfers, not on a branch. |
| **User reloaded from the database on every request** (not trusted from the JWT) | Deactivation and role changes take effect immediately. | One primary-key lookup per request. |
| **Synchronous SQLAlchemy** | Simpler, clearer transaction and locking code; FastAPI still serves requests concurrently in a thread pool. | Lower ceiling than async at very high concurrency. |
| **Tests against real PostgreSQL**, schema built by the real migrations | Locks, constraints and triggers are what is being tested; SQLite would hide them. | Tests need a database server. |

## 4. How each threat is handled

| Threat | Defence | Proof (test) |
|---|---|---|
| Concurrent requests oversell | Row lock + check + CHECK constraint | 6/5/4 race ×20, 2-request race ×20, 25 buyers for 10 units, HTTP-level race |
| A bug bypasses the service checks | Database CHECK constraints → `409 constraint_violation`, full rollback | `test_database_constraint_stops_oversell_even_if_code_checks_fail` |
| Client retries a create | Idempotency key (also under concurrent duplicates) | `test_retried_create_with_same_key_returns_same_order`, `test_concurrent_duplicate_requests_with_one_idempotency_key` |
| Client retries a transition | Idempotent transitions under an order-row lock | `test_repeated_reserve_does_not_double_reserve`, `test_cancelling_twice_releases_only_once` |
| Failure half-way through | One transaction per request; services don't commit | `test_crash_mid_transaction_leaves_no_partial_changes` (crash injected after the first line) |
| Conflicting transitions at once | Order/transfer row lock | `test_cancel_and_fulfil_racing_on_one_order` |
| Acting outside one's branch | Branch-scope check in every service; audited | `test_staff_cannot_*`, `test_manager_cannot_*`, transfer side checks |
| Tampering with history | Append-only triggers on ledger and audit log | `test_stock_ledger_rejects_update`, `test_audit_log_is_append_only` |
| Ledger drift | Ledger written in the same transaction as every change | `ledger_matches_inventory()` asserted throughout the suite |

## 5. Roles

Admin (everything, all branches), Branch Manager (runs one branch: adjusts stock, approves and dispatches
transfers, reads its users and audit log) and Staff (serves customers: orders, receiving transfers).
Full matrix and reasoning: [ROLES.md](ROLES.md).

## 6. Assumptions

- The product catalogue is shared by all branches; stock is held per branch.
- Admins are global and belong to no branch; every other user belongs to exactly one branch.
- Quantities are whole units; one currency; prices are `NUMERIC(12,2)` and snapshotted on order lines.
- Confirmed orders can still be cancelled (the goods haven't left); fulfilled orders can't.
- Staff can't adjust stock: an adjustment with no order or transfer behind it is the easiest way to hide loss, so it is limited to managers.
- Transfers are approved by the source branch (it's their stock) and received by the destination.
- Nothing is hard-deleted: users, branches and products are deactivated so history keeps pointing at them.
- A branch can't be deactivated while it has reserved stock, open orders or open transfers.
- Failed requests are not cached under their idempotency key, so a retry after restocking can succeed.

## 7. Known limitations and what I'd do next

| Limitation | Production approach |
|---|---|
| No refresh tokens or token revocation list; a token stays valid until it expires (30 min) unless the user is deactivated | Short-lived access + refresh tokens; revocation list in Redis |
| No rate limiting or lockout on repeated failed logins (failures are audited) | Rate limiting at the gateway or with Redis |
| Idempotency keys never expire | Scheduled cleanup after e.g. 24 hours |
| A very popular product is a single hot row | Shard stock into buckets, or queue reservations |
| No partial receipt or "damaged in transit" on transfers | Received quantity per line, with the difference written off and audited |
| No returns process after fulfilment | A return flow creating compensating ledger entries |
| No reporting endpoints (e.g. stock valuation, in-transit totals per branch) | Read-model queries or a reporting replica |
| Single-currency pricing, no tax | Currency and tax per branch |

## 8. Delivery

| Day | Delivered |
|---|---|
| 1 | Data model and migration, append-only triggers, project structure, configuration, JWT authentication, RBAC, user management, error handling |
| 2 | Morning: branches and products. Evening: the stock engine, adjustments, stock ledger, audit-log reading, seed data |
| 3 | Morning: orders, their state machine and idempotency. Evening: the concurrency challenge, its tests and the live race script |
| 4 | Morning: transfers and their state machine. Evening: final test scenarios, API documentation and OpenAPI export, demo scripts, README, this report |
