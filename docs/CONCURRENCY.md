# Concurrency, idempotency and rollback

This document answers the task's concurrency challenge: **which mechanism, why,
where the transaction boundaries are, and how duplicates are handled**, and
shows how to reproduce the proof.

## The scenario

> Branch A has 10 units. Three reservations for 6, 5 and 4 arrive at the same
> moment. Total reserved must never exceed 10, and failed requests must leave
> no partial reservation.

## What goes wrong without protection

Each request does *read → check → write*:

```
Request 6:  read available = 10 → 10 ≥ 6 ✓ → write reserved = 6
Request 5:  read available = 10 → 10 ≥ 5 ✓ → write reserved = 5   ← read before 6 was written
Request 4:  read available = 10 → 10 ≥ 4 ✓ → write reserved = 4
```

All three succeed: **15 units promised out of 10**. Worse, each one *overwrites*
the others' write (a "lost update"), so the database ends up showing
`reserved = 4` while customers were promised 15.

This was verified, not assumed: with the row lock temporarily removed, the
concurrency test fails immediately with `oversold {4, 5, 6}`.

> Note: the `CHECK (reserved <= on_hand)` constraint does **not** catch this.
> Each individual write (6, 5 or 4) is valid on its own; the problem is that they
> were computed from a stale read. Constraints protect against impossible
> *values*; locking protects against *interleaving*. We need both.

## The mechanism: pessimistic row locking (`SELECT … FOR UPDATE`)

Every operation that changes stock runs inside **one database transaction**:

```sql
BEGIN;
  -- 1. Lock the inventory row(s). A second transaction asking for the same row
  --    WAITS here until the first one commits or rolls back.
  SELECT * FROM inventory
   WHERE branch_id = :branch AND product_id IN (:products)
   ORDER BY product_id
   FOR UPDATE;

  -- 2. Check every line BEFORE changing anything.
  --    If any line is short → raise → ROLLBACK (nothing was changed).

  -- 3. Apply the change and record it in the ledger.
  UPDATE inventory SET reserved = reserved + :qty WHERE id = :row;
  INSERT INTO stock_movements (...);           -- one per line
  INSERT INTO orders / order_items (...);      -- the order itself
  INSERT INTO audit_logs (...);
COMMIT;
```

With the lock, the three requests are forced to take turns:

```
Request 6:  LOCK ✓ → available 10 → reserve 6 → COMMIT (unlock)
Request 5:  LOCK (waited) → available 4 → 4 < 5 ✗ → ROLLBACK, 409 insufficient_stock
Request 4:  LOCK (waited) → available 4 → reserve 4 → COMMIT
Result: reserved = 10, never more.
```

When a waiting transaction finally gets the lock, PostgreSQL gives it the
**latest committed** row, so its check uses up-to-date numbers.

Code: [`app/services/stock.py`](../app/services/stock.py) (`lock_inventory`,
`ensure_available`, `apply_movement`) and
[`app/services/order_service.py`](../app/services/order_service.py) (`_reserve`).

### Why this mechanism, and not the alternatives

| Option | How it works | Why not chosen |
|---|---|---|
| **`SELECT … FOR UPDATE` (chosen)** | Lock rows, check, update | Simple to reason about; handles **multi-line orders** (check all lines, then apply all) naturally; waiting is short because transactions are short. |
| Conditional update `UPDATE … SET reserved = reserved + n WHERE on_hand - reserved >= n` | One atomic statement per row; 0 rows updated = not enough | Excellent for single-line updates, but a multi-line order needs a check after every line plus a manual rollback, and it can't report *every* short line at once. |
| Optimistic locking (version column, retry on conflict) | Read freely, fail on write if the version changed | Under heavy contention for a popular item most requests fail and must be retried by the client; more complex for no gain here. |
| `SERIALIZABLE` isolation | Database aborts conflicting transactions | Correct, but every conflict becomes a retryable error; the application would need a retry loop everywhere. |
| Application lock (threading.Lock / Redis) | Lock in Python or an external store | A Python lock doesn't work across several server processes; Redis adds infrastructure and a second source of truth. The database already owns the data. |

### Deadlock prevention

An order for products A and B, and another for B and A, could otherwise lock one
row each and wait for each other forever. Rows are **always locked in product-id
order** (`ORDER BY product_id`), and an order's own row is always locked
*before* its inventory rows. With one global order, no cycle (and so no deadlock)
between our own transactions is possible.

### Three layers of protection

1. **Row lock + check** in the service gives the correct answer and a clear 409.
2. **`apply_movement`** refuses any change that would make a row invalid.
3. **Database CHECK constraints** (`on_hand >= 0`, `0 <= reserved <= on_hand`)
   reject an impossible value even if the code above had a bug.

## Transaction boundaries

| Operation | One transaction contains |
|---|---|
| Create order (`reserve: true`) | order + items + inventory locks + reservations + ledger rows + audit row + idempotency key |
| Reserve / fulfil / cancel | order row lock + inventory locks + stock changes + ledger rows + status change + audit row |
| Manual adjustment | inventory lock + change + ledger row + audit row + idempotency key |

The service layer never commits; the API route commits once at the end. If
anything raises in between (a business rule, a bug, a lost connection), the
request's database session is rolled back and **nothing** from that request
survives. The only exception is deliberate: failed logins, denied access, and
failed reservations are audited on a *separate* connection so the evidence
survives the rollback.

Proven by `test_crash_mid_transaction_leaves_no_partial_changes`, which makes the
second of two stock movements crash on purpose. The first one is rolled back too.

## Duplicate and retried requests

Two kinds of protection, for two kinds of retries.

### 1. State transitions are naturally idempotent

`POST /orders/{id}/reserve` on an order that is **already reserved** returns the
order unchanged (200). It does not reserve again. The same applies to confirm,
fulfil and cancel. The order row is locked during the transition, so even two
simultaneous `reserve` calls on the same order reserve once.

### 2. Creates and adjustments use an `Idempotency-Key`

Creating an order or adjusting stock is not naturally idempotent: sending it twice
would do it twice. The client sends a unique `Idempotency-Key` header (e.g. a UUID)
and re-sends the **same** key when retrying.

```
Idempotency-Key: 7d6f4c1e-2b9a-4e0f-8c31-5a2d9e7b6f10
```

| Situation | Result |
|---|---|
| First request | Work is done; key + response stored in `idempotency_keys` in the **same transaction** |
| Retry with same key and same body | Stored response returned with header `Idempotent-Replayed: true`; nothing is done again |
| Same key, different body | 422 `idempotency_key_reused` (client bug) |
| Two copies arrive **at the same time** | The unique index on `(user_id, key)` makes the second `INSERT` wait for the first; it then replays the first's response. Exactly one order. |
| First request **failed** (e.g. 409) | Its transaction rolled back, key included. A retry runs fresh and may succeed after restocking. |

Keys are scoped per user, so two users can't collide or read each other's responses.

Code: [`app/services/idempotency.py`](../app/services/idempotency.py).

## Reproducing the proof

**Automated tests** (real threads, real PostgreSQL):

```bash
pytest tests/concurrency -v -s
```

| Test | What it proves |
|---|---|
| `test_two_concurrent_requests_competing_for_limited_stock` | Two requests (7 and 6) for 10 units at the same instant, repeated 20 times: exactly one wins every time. |
| `test_three_simultaneous_reservations_never_oversell` | The task's 6/5/4 scenario, repeated 20 times with a `threading.Barrier` releasing all three at once. Checks total ≤ 10, outcome ∈ {6,4}, {5,4}, {6}, {5}, {4}, inventory = sum of winners, no order or ledger rows for losers. |
| `test_many_buyers_for_the_last_units` | 25 simultaneous buyers for 10 units: exactly 10 succeed. |
| `test_second_transaction_waits_for_the_row_lock` | Shows the mechanism directly: while one transaction holds the lock, another blocks, then re-checks against the committed numbers. |
| `test_concurrent_duplicate_requests_with_one_idempotency_key` | 5 simultaneous copies of one request create one order. |
| `test_cancel_and_fulfil_racing_on_one_order` | Cancel and fulfil at the same time: exactly one wins, stock matches the winner. |
| `test_race_through_the_http_api` | The 6/5/4 race through the full HTTP stack. |

**Live, against a running server** (for the demo):

```bash
python -m scripts.seed
uvicorn app.main:app
python -m scripts.concurrency_demo --rounds 3     # in a second terminal
```

Example output:

```
Round 2: DEMO-942AAE at Kandy, 10 units on hand; requests for 6, 5 and 4 sent simultaneously
  6 units  -> 201 RESERVED   order cd3498bc…  (33 ms)
  5 units  -> 409 insufficient_stock (only 4 left)  (36 ms)
  4 units  -> 201 RESERVED   order d6f9ee2b…  (43 ms)
  Result: on_hand=10 reserved=10 available=0  (reserved by winners: 10)  OK
```

## Limitations

- A very popular product becomes a hot row: requests for it are processed one at a
  time. That's correct behaviour, and transactions are kept short (milliseconds), but
  at very high volume one would shard stock or move to a reservation queue.
- Idempotency keys are kept forever. A production system would expire them (e.g. after 24 hours) with a scheduled cleanup.
- Failed requests aren't cached under their idempotency key (by design, see above).
