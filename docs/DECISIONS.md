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

## Assumptions
- A product catalogue is shared by all branches; stock is per branch.
- Admins are global and belong to no branch; every other user belongs to exactly one.
- Stock quantities are whole units (integers).
- One currency; prices are stored as `NUMERIC(12,2)`.
