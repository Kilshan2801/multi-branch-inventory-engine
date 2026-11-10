# API reference

Base URL: `http://localhost:8000/api/v1`

- **Interactive docs (Swagger UI):** <http://localhost:8000/docs>. Try every endpoint in the browser.
- **OpenAPI spec:** [`docs/openapi.json`](openapi.json) (regenerate with `python -m scripts.export_openapi`).
  **Postman:** *Import → File →* `docs/openapi.json` creates a collection with every endpoint.
- All request and response bodies are JSON. IDs are UUIDs. Money is a decimal **string** (`"24999.00"`) so it never loses precision.

## Authentication

Every endpoint except `POST /auth/login` and `GET /health` needs a bearer token:

```
Authorization: Bearer <access_token>
```

### `POST /auth/login`

```json
{ "email": "staff.kdy@example.com", "password": "••••••••" }
```

`200 OK`

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {
    "id": "839380f4-7695-4406-855b-4383121b5473",
    "email": "staff.kdy@example.com",
    "full_name": "KDY Staff Member",
    "role": "staff",
    "branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
    "is_active": true,
    "created_at": "2026-11-07T18:34:34.760395+05:30",
    "updated_at": "2026-11-07T18:34:34.760395+05:30"
  }
}
```

Wrong email and wrong password both return the same `401 invalid_credentials`.

### `GET /auth/me`
The signed-in user plus the list of permissions their role grants.

## Errors

Every error has the same shape, whatever went wrong:

```json
{
  "error": {
    "code": "invalid_transition",
    "message": "Cannot fulfill an order that is reserved",
    "details": { "current_status": "reserved", "requested": "fulfill", "allowed_actions": ["cancel", "confirm"] }
  },
  "request_id": "26fd77b71a8948e18f2012e1f43c7f88"
}
```

`request_id` is also returned in the `X-Request-ID` header and stored with audit entries, so an error
can be traced to its log lines. Clients may send their own `X-Request-ID`.

| Status | `error.code` values | Meaning |
|---|---|---|
| 401 | `missing_token`, `invalid_token`, `token_expired`, `invalid_credentials` | Not signed in |
| 403 | `permission_denied`, `branch_access_denied` | Role can't do this, or wrong branch |
| 404 | `not_found`, `inventory_not_found` | No such resource |
| 409 | `insufficient_stock`, `invalid_transition`, `constraint_violation`, `sku_taken`, `branch_code_taken`, `email_taken`, `branch_has_open_work`, `self_lockout` | Clashes with current state |
| 422 | `validation_error`, `product_inactive`, `product_not_found`, `branch_inactive`, `branch_not_found`, `idempotency_key_reused`, `empty_update`, `invalid_role_branch` | Invalid input |
| 500 | `internal_error` | Unexpected; details are logged, never returned |

Validation errors list every bad field:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "details": [
      { "field": "body.customer_name", "message": "String should have at least 1 character", "type": "string_too_short" },
      { "field": "body.items", "message": "List should have at least 1 item after validation, not 0", "type": "too_short" }
    ]
  },
  "request_id": "6b0ed047a26d41449d19cd14620fcd72"
}
```

## Retries: the `Idempotency-Key` header

`POST /orders`, `POST /transfers` and `POST /branches/{id}/inventory/adjustments` accept an optional
`Idempotency-Key` header (1–128 characters, e.g. a UUID). Retrying with the same key returns the original
response with `Idempotent-Replayed: true` instead of doing the work twice. The same key with a different
body is `422 idempotency_key_reused`. State transitions (`/reserve`, `/cancel`, …) need no key: repeating
one that already happened returns the current state unchanged. Details: [CONCURRENCY.md](CONCURRENCY.md).

## Lists and pagination

List endpoints return:

```json
{ "items": [ ... ], "total": 42, "limit": 50, "offset": 0 }
```

Query parameters: `limit` (1–100, default 50) and `offset` (default 0), plus the filters listed per endpoint.

---

## Endpoints

"Own branch" means branch managers and staff are limited to the branch they belong to; admins can act on any branch.
Full permission matrix: [ROLES.md](ROLES.md).

### Users

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/users` | admin | `{email, full_name, password, role, branch_id}`. Admins have no branch; others must have one. |
| GET | `/users` | admin; manager (own branch) | filters: `role`, `branch_id`, `is_active` |
| GET | `/users/{id}` | admin; manager (own branch) | |
| PATCH | `/users/{id}` | admin | any of `full_name, role, branch_id, is_active, password`. `is_active: false` deactivates. |

### Branches

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/branches` | admin | `{"code": "NGB", "name": "Negombo Branch", "address": "..."}`. Code stored upper-case, unique. |
| GET | `/branches` | everyone | filter: `is_active` |
| GET | `/branches/{id}` | everyone | |
| PATCH | `/branches/{id}` | admin | `name, address, is_active`. Deactivation refused (409) while the branch has reserved stock, open orders or open transfers. |

### Products

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/products` | admin | `{"sku": "PHN-001", "name": "Smartphone X", "description": "...", "unit_price": "89999.00"}` |
| GET | `/products` | everyone | filters: `search` (SKU or name), `is_active` |
| GET | `/products/{id}` | everyone | |
| PATCH | `/products/{id}` | admin | `name, description, unit_price, is_active`. SKU never changes. |

### Inventory

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/branches/{id}/inventory` | own branch | filters: `search`, `in_stock_only` |
| GET | `/branches/{id}/inventory/{product_id}` | own branch | |
| POST | `/branches/{id}/inventory/adjustments` | manager (own), admin | Idempotency-Key supported |
| GET | `/branches/{id}/stock-movements` | own branch | the ledger, newest first; filters: `product_id`, `movement_type`, `reference_type`, `reference_id` |

`GET /branches/{id}/inventory/{product_id}` → `200`

```json
{
  "branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
  "product_id": "9bfc95c1-3f1c-44e4-969f-e4ae43e625d6",
  "sku": "HDP-001",
  "product_name": "Wireless Headphones",
  "on_hand": 20,
  "reserved": 0,
  "available": 20,
  "updated_at": "2026-11-07T18:34:34.760395+05:30"
}
```

`POST /branches/{id}/inventory/adjustments`

```json
{ "product_id": "9bfc95c1-3f1c-44e4-969f-e4ae43e625d6", "quantity_delta": 5, "reason": "Goods received, supplier invoice INV-1042" }
```

`201 Created`: the new stock level and the ledger entry that was written:

```json
{
  "inventory": { "sku": "HDP-001", "on_hand": 25, "reserved": 2, "available": 23, "...": "..." },
  "movement": {
    "id": "106feb0d-7074-4a30-8fed-410c9de4efe7",
    "movement_type": "adjustment",
    "on_hand_delta": 5,
    "reserved_delta": 0,
    "on_hand_after": 25,
    "reserved_after": 2,
    "reason": "Goods received, supplier invoice INV-1042",
    "reference_type": "manual",
    "reference_id": null,
    "actor_id": "fd8aa8a6-e264-4a03-b48c-20ae87a2b2f5",
    "created_at": "2026-11-07T18:35:25.166620+05:30"
  }
}
```

Removing more than the unreserved quantity → `409 insufficient_stock`.

### Orders

| Method | Path | Who | Effect |
|---|---|---|---|
| POST | `/orders` | own branch | create **pending**; with `"reserve": true` also reserve (all lines or nothing). Idempotency-Key supported. |
| GET | `/orders` | own branch | filters: `branch_id`, `status` |
| GET | `/orders/{id}` | own branch | |
| POST | `/orders/{id}/reserve` | own branch | pending → reserved; `reserved += qty` |
| POST | `/orders/{id}/confirm` | own branch | reserved → confirmed |
| POST | `/orders/{id}/fulfill` | own branch | confirmed → fulfilled; `on_hand -= qty`, `reserved -= qty` |
| POST | `/orders/{id}/cancel` | own branch | pending/reserved/confirmed → cancelled; releases what the order holds. Optional `{"reason": "..."}` |

`POST /orders` (header `Idempotency-Key: 5f0c…`)

```json
{
  "branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
  "customer_name": "Saman Kumara",
  "customer_contact": "+94 77 123 4567",
  "reserve": true,
  "items": [{ "product_id": "9bfc95c1-3f1c-44e4-969f-e4ae43e625d6", "quantity": 2 }]
}
```

`201 Created`

```json
{
  "id": "a276ae40-81d1-47db-84e2-1bb8c04d0362",
  "branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
  "status": "reserved",
  "customer_name": "Saman Kumara",
  "customer_contact": "+94 77 123 4567",
  "notes": null,
  "items": [
    { "product_id": "9bfc95c1-3f1c-44e4-969f-e4ae43e625d6", "quantity": 2, "unit_price": "24999.00", "line_total": "49998.00" }
  ],
  "total_amount": "49998.00",
  "allowed_actions": ["cancel", "confirm"],
  "created_by": "839380f4-7695-4406-855b-4383121b5473",
  "cancellation_reason": null,
  "created_at": "2026-11-07T18:35:25.104090+05:30",
  "updated_at": "2026-11-07T18:35:25.104090+05:30",
  "reserved_at": "2026-11-07T18:35:25.119708+05:30",
  "confirmed_at": null,
  "fulfilled_at": null,
  "cancelled_at": null
}
```

Not enough stock → `409`:

```json
{
  "error": {
    "code": "insufficient_stock",
    "message": "Not enough available stock",
    "details": {
      "branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
      "shortages": [{ "product_id": "c986e500-4e6a-485b-85fd-18b53989ded1", "requested": 999, "available": 8 }]
    }
  },
  "request_id": "..."
}
```

### Transfers

| Method | Path | Who | Effect |
|---|---|---|---|
| POST | `/transfers` | manager of source or destination, admin | create **requested**. Idempotency-Key supported. |
| GET | `/transfers` | source or destination branch | filters: `branch_id`, `status` (`dispatched` = in transit) |
| GET | `/transfers/{id}` | source or destination branch | |
| POST | `/transfers/{id}/approve` | source manager | requested → approved; source `reserved += qty` (all lines or nothing) |
| POST | `/transfers/{id}/reject` | source manager | requested → rejected; body `{"reason": "..."}` required |
| POST | `/transfers/{id}/dispatch` | source manager | approved → dispatched; source `on_hand -= qty`, `reserved -= qty` |
| POST | `/transfers/{id}/receive` | destination manager or staff | dispatched → received; destination `on_hand += qty` |
| POST | `/transfers/{id}/cancel` | manager of either branch | requested/approved → cancelled; releases the reservation. Optional `{"reason": "..."}` |

`POST /transfers`

```json
{
  "source_branch_id": "248bf00b-9f78-48a7-973d-0416606d2497",
  "destination_branch_id": "0b7e4f2a-5c1d-4e8f-9a3b-6d2c1e0f9a8b",
  "items": [{ "product_id": "1f2e3d4c-5b6a-4978-8695-a4b3c2d1e0f9", "quantity": 3 }],
  "notes": "Colombo is out of stock for the weekend sale"
}
```

`201 Created`: the response has the same fields as `GET /transfers/{id}`: `status`, `items`,
`allowed_actions`, `requested_by` / `approved_by` / `dispatched_by` / `received_by` / `closed_by` and their timestamps.

### Audit log

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/audit-logs` | admin; manager (own branch) | newest first; filters: `action` (exact or prefix, e.g. `order`), `outcome`, `actor_id`, `entity_type`, `entity_id`, `branch_id`, `since`, `until` |

Recorded actions: `auth.login`, `access.denied`, `user.*`, `branch.*`, `product.*`, `inventory.adjust`,
`order.create|reserve|confirm|fulfill|cancel`, `transfer.request|approve|reject|dispatch|receive|cancel`.
Failed logins, denied access, failed reservations and invalid transitions are recorded with outcome `failure` or `denied`.

### Health

`GET /health` → `{"status": "ok", "database": "ok"}` (no authentication).
