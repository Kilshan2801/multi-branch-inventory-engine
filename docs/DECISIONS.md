# Design decisions and assumptions

A running log; each entry says what was chosen, why, and what was given up.
This feeds the final technical report.

## Day 1: foundation

### D1. FastAPI + PostgreSQL + SQLAlchemy 2.0 (synchronous) + Alembic
- **Why FastAPI:** request validation and OpenAPI/Swagger docs come from the same type hints, so the docs cannot drift from the code.
- **Why PostgreSQL:** reliable row-level locking (`SELECT … FOR UPDATE`), CHECK constraints, generated columns, triggers and JSONB, which this project needs.
- **Why synchronous SQLAlchemy:** FastAPI runs sync endpoints in a thread pool, so requests still run concurrently. Sync code is simpler to reason about for transactions and locking. *Trade-off:* lower ceiling on concurrent connections than async; irrelevant at this scale.

### D2. Layered structure
```
api/routes   HTTP only: parse input, call a service, shape the response
services     business rules and transaction boundaries (commit lives here)
models       tables and database-level constraints
core         config, security, permissions, errors (no business logic)
```
Routes never touch the database directly, so business rules can be tested and reused independently of HTTP.

### D3. Integrity enforced in the database, not only in Python
CHECK constraints (`reserved ≤ on_hand`, quantities > 0, …), unique constraints and
foreign keys mean the database rejects bad data even if application code is wrong
or two requests race. Application checks exist to give friendly errors; the database
is the final guarantee.

### D4. `available` is a generated column
`available = on_hand − reserved` is computed by PostgreSQL. Storing it by hand would allow it to drift.

### D5. Append-only ledger and audit log (trigger-enforced)
`stock_movements` and `audit_logs` reject `UPDATE` and `DELETE` at the database level.
Corrections are new rows. *Trade-off:* `TRUNCATE` is still possible for a superuser (used by the test suite to reset).

### D6. UUID primary keys
Stable, never reused, safe to expose in URLs (not guessable or countable like 1, 2, 3).
*Trade-off:* larger than integers; negligible here.

### D7. Enums stored as VARCHAR + CHECK, not native PostgreSQL ENUM types
Adding a status later is a simple constraint change. Native enum types are awkward to alter inside a transaction.

### D8. Soft delete only
Users, branches and products are deactivated (`is_active = false`), never deleted, because ledger and audit rows reference them (`ON DELETE RESTRICT`).

### D9. JWT access tokens, user reloaded on every request
- HS256, 30-minute lifetime (configurable), algorithm pinned on decode (blocks `alg: none` attacks).
- The token only identifies the user. Role, branch and active status are read from the database on every request, so **deactivation and role changes take effect immediately**. *Trade-off:* one primary-key lookup per request.
- *Known limitation:* no refresh tokens and no server-side logout/revocation list; a token stays valid until it expires unless the user is deactivated.

### D10. Login hardening
- bcrypt password hashing (work factor 12).
- Same error message and similar timing for "unknown email" and "wrong password" (prevents user enumeration).
- Every login attempt (success and failure, with reason) is audited.
- *Known limitation:* no rate limiting or lockout after repeated failures. In production this belongs at the gateway or in a shared store like Redis.

### D11. Two-step authorization: permission, then branch scope
See [ROLES.md](ROLES.md). Role → permission mapping lives in one file; branch checks in one helper. Denials are audited.

### D12. Audit entries: inside the transaction or independent
Successful changes write their audit row in the **same transaction** as the change (both commit or neither).
Failed logins and access denials are written on a **separate connection** that commits immediately, because the request itself is rejected.

### D13. One error shape for every response
`{"error": {"code", "message", "details"}, "request_id"}`. `code` is stable for clients to branch on; `request_id` links an error to its log and audit entries.

| Status | Meaning |
|---|---|
| 400 | malformed request |
| 401 | not authenticated (missing / invalid / expired token, bad credentials) |
| 403 | authenticated but not allowed (role or branch) |
| 404 | resource not found |
| 409 | conflicts with current state (duplicate, insufficient stock, invalid transition) |
| 422 | well-formed but invalid input |
| 500 | unexpected error (details logged, not exposed) |

### D14. Tests run against real PostgreSQL
SQLite does not support row locking the same way and would not exercise the CHECK constraints and triggers that this design relies on. The test database is built with the real migrations.

## Day 2: branches, products, inventory and the ledger

### D15. Precise ledger timestamps (migration 0002)
`now()` in PostgreSQL is the transaction's start time, so several movements written by one request
would share one timestamp. `clock_timestamp()` records the real time of each insert. Done as a
second migration rather than editing the first, as a real deployed project would have to.

### D16. Branch directory is visible to everyone signed in
Staff and managers need to know the other branches (ids, codes, names) to coordinate stock between them.
Branch *data* (stock, orders, users) stays restricted to the branch.

### D17. Deactivation rules
- A **branch** can't be deactivated while it has reserved stock, open orders, or open transfers; those would be stranded.
- A **product** can always be deactivated. It then can't be ordered, transferred or stocked in,
  but existing reservations can still be completed or cancelled, and remaining stock can be written off.

### D18. One stock engine (`app/services/stock.py`)
Every stock change goes through three functions:
`lock_inventory` (SELECT … FOR UPDATE), `ensure_available` (check every line before changing any),
and `apply_movement` (change the row + write the ledger entry). Keeping this in one place means
the locking and ledger rules can't be forgotten by a new feature.

### D19. Lock rows in a fixed order (sorted by product id)
If two requests both need products A and B, both lock A first, then B. Neither can end up holding
B while waiting for A, which rules out deadlocks between our own transactions.

### D20. Three layers of protection against invalid stock
1. Service checks give a clear error (`insufficient_stock` with every short line listed).
2. `apply_movement` refuses any change that would make a row invalid.
3. Database CHECK constraints reject it even if both of the above had a bug.

### D21. Manual adjustments
- Positive = goods received or count correction; negative = damage, loss or correction.
- A reason is mandatory and goes into the ledger.
- **Reserved units cannot be removed.** Removing them would silently break a promise made to a customer or another branch.
- Only branch managers (own branch) and admins may adjust (see ROLES.md).
- A product that has never been stocked at a branch gets its inventory row created on its first positive adjustment.

## Day 3: orders, concurrency and idempotency

Full explanation of the concurrency design: [CONCURRENCY.md](CONCURRENCY.md).

### D22. Order state machine
`pending → reserved → confirmed → fulfilled`, with `cancelled` reachable from pending, reserved and confirmed.
- **Reserve** holds stock; **fulfil** takes it out of the branch; **cancel** releases it.
- **Confirmed orders can still be cancelled** (e.g. payment reversed) because the goods haven't left. Fulfilled orders can't: the goods are gone, so that would need a returns process (out of scope).
- `pending → confirmed` is not allowed: an order can't be confirmed without stock behind it.
- Each response includes `allowed_actions`, so clients don't need to hard-code the rules.

### D23. Create-and-reserve in one call (`"reserve": true`)
Saves a round trip and matches the task's scenario ("reservation requests arrive"). If any line
is short, **no order is created at all**, so there are no orphaned pending orders from failed attempts.
Creating as pending and calling `/reserve` later is also supported; a failed reserve leaves the order pending.

### D24. Repeating a completed transition is a no-op, not an error
`reserve` on a reserved order returns 200 with the unchanged order. A client retrying after a
timeout can't tell whether its first call worked, and it shouldn't get an error for a request
that effectively succeeded. Transitions that are actually invalid (e.g. cancel a fulfilled order) return 409.

### D25. Cancellation releases what the ledger says, not what the order says
The amount released is the sum of this order's `reserved_delta` in `stock_movements`.
It's equal to the order quantities in practice, but deriving it from the ledger guarantees
"release exactly what was reserved", even if a release is ever repeated or a reservation was partial.

### D26. Idempotency keys for non-idempotent writes
Order creation and stock adjustments accept an `Idempotency-Key` header. The key is stored in the
same transaction as the work, so a key exists only if the work committed. A unique index makes
concurrent duplicates wait for, then replay, the first. Failures are not cached.

### D27. Prices are snapshotted on order lines
`order_items.unit_price` is copied from the product at order time, so later price changes don't alter existing orders.

### D28. Failed attempts are audited even though they roll back
Failed reservations and invalid transitions are written to the audit log on a separate connection.

### D29. Pessimistic row locking for stock (`SELECT … FOR UPDATE`)
Chosen over conditional updates, optimistic locking, SERIALIZABLE and application locks
(comparison table in CONCURRENCY.md). Verified by removing the lock: the race test then
oversold 15 of 10 units.

## Day 4: transfers

### D30. Transfer state machine
`requested → approved → dispatched → received`, plus `rejected` (from requested) and `cancelled` (from requested or approved).

| Step | Who (besides admin) | Stock effect |
|---|---|---|
| request | manager of source **or** destination | none |
| approve | source manager | source `reserved += qty` (all lines or none) |
| reject | source manager, reason required | none |
| dispatch | source manager | source `on_hand -= qty`, `reserved -= qty` → in transit |
| receive | destination manager **or staff** | destination `on_hand += qty` |
| cancel | manager of either branch, before dispatch | source reservation released |

### D31. Approval reserves stock; dispatch can never exceed it
Reserving at approval time means the units can't be sold to customers or written off between approval
and dispatch, so a dispatch can never fail for lack of stock and can never send more than was there.
"Over-dispatch" is stopped at approval with `409 insufficient_stock`.

### D32. Stock is "in transit" between dispatch and receipt
Dispatch takes units out of the source's `on_hand`; receipt adds them at the destination. In between, they
are the quantities on `dispatched` transfers (`GET /transfers?status=dispatched`). This models reality:
the goods are on a truck, not on either shelf. Conservation holds at every moment:
`Σ on_hand (all branches) + Σ in-transit = constant`, which is checked in the tests.

### D33. Receipt adds exactly what was dispatched (read from the ledger)
The destination receives the sum of the transfer's `transfer_out` movements, not a number re-read from
the request, so the two sides of a transfer always match.

### D34. No cancellation after dispatch
Once goods have physically left, "cancel" would make units vanish. Undoing a dispatched transfer means
receiving it and creating a return transfer, which keeps both movements visible in the ledger.
*Known limitation:* no partial receipt or damaged-in-transit handling; a receiving branch records damage
afterwards with a negative adjustment.

## Assumptions
- A product catalogue is shared by all branches; stock is per branch.
- Admins are global and belong to no branch; every other user belongs to exactly one.
- Stock quantities are whole units (integers).
- One currency; prices are stored as `NUMERIC(12,2)`.
