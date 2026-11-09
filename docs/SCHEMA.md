# Database schema

PostgreSQL (tested on 16 and 18). Created by the Alembic migrations in [`alembic/versions/`](../alembic/versions/):
`0001` creates every table, constraint, index and trigger; `0002` switches the ledger and audit
timestamps to `clock_timestamp()`. All primary keys are UUIDs. All timestamps are `timestamptz`.

## Entity–relationship diagram

```mermaid
erDiagram
    BRANCHES ||--o{ USERS : "employs (non-admins)"
    BRANCHES ||--o{ INVENTORY : stocks
    PRODUCTS ||--o{ INVENTORY : "stocked as"
    BRANCHES ||--o{ STOCK_MOVEMENTS : ""
    PRODUCTS ||--o{ STOCK_MOVEMENTS : ""
    USERS ||--o{ STOCK_MOVEMENTS : "performed by"
    BRANCHES ||--o{ ORDERS : receives
    USERS ||--o{ ORDERS : "created by"
    ORDERS ||--|{ ORDER_ITEMS : contains
    PRODUCTS ||--o{ ORDER_ITEMS : ""
    BRANCHES ||--o{ TRANSFERS : "source / destination"
    USERS ||--o{ TRANSFERS : "requested / approved / dispatched / received by"
    TRANSFERS ||--|{ TRANSFER_ITEMS : contains
    PRODUCTS ||--o{ TRANSFER_ITEMS : ""
    USERS ||--o{ AUDIT_LOGS : actor
    USERS ||--o{ IDEMPOTENCY_KEYS : owns

    BRANCHES {
        uuid id PK
        varchar code UK
        varchar name
        varchar address
        bool is_active
    }
    USERS {
        uuid id PK
        varchar email UK "lowercase"
        varchar full_name
        varchar password_hash "bcrypt"
        varchar role "admin | branch_manager | staff"
        uuid branch_id FK "NULL only for admin"
        bool is_active
    }
    PRODUCTS {
        uuid id PK
        varchar sku UK
        varchar name
        text description
        numeric unit_price ">= 0"
        bool is_active
    }
    INVENTORY {
        uuid id PK
        uuid branch_id FK
        uuid product_id FK
        int on_hand ">= 0"
        int reserved ">= 0 and <= on_hand"
        int available "GENERATED: on_hand - reserved"
    }
    STOCK_MOVEMENTS {
        uuid id PK
        uuid branch_id FK
        uuid product_id FK
        varchar movement_type
        int on_hand_delta
        int reserved_delta
        int on_hand_after
        int reserved_after
        text reason "required"
        varchar reference_type "order | transfer | manual"
        uuid reference_id
        uuid actor_id FK
        timestamptz created_at
    }
    ORDERS {
        uuid id PK
        uuid branch_id FK
        varchar status
        varchar customer_name
        uuid created_by FK
        text cancellation_reason
        timestamptz reserved_at
        timestamptz confirmed_at
        timestamptz fulfilled_at
        timestamptz cancelled_at
    }
    ORDER_ITEMS {
        uuid id PK
        uuid order_id FK
        uuid product_id FK
        int quantity "> 0"
        numeric unit_price "snapshot"
    }
    TRANSFERS {
        uuid id PK
        uuid source_branch_id FK
        uuid destination_branch_id FK "<> source"
        varchar status
        text status_reason
        uuid requested_by FK
        uuid approved_by FK
        uuid dispatched_by FK
        uuid received_by FK
        uuid closed_by FK
    }
    TRANSFER_ITEMS {
        uuid id PK
        uuid transfer_id FK
        uuid product_id FK
        int quantity "> 0"
    }
    AUDIT_LOGS {
        uuid id PK
        uuid actor_id FK
        varchar action
        varchar outcome "success | failure | denied"
        varchar entity_type
        varchar entity_id
        uuid branch_id
        varchar ip_address
        varchar request_id
        jsonb details
        timestamptz created_at
    }
    IDEMPOTENCY_KEYS {
        uuid id PK
        uuid user_id FK
        varchar key "unique per user"
        varchar endpoint
        varchar request_hash
        int response_status
        jsonb response_body
    }
```

## Tables at a glance

| Table | Purpose | Key integrity rules |
|---|---|---|
| `branches` | Shops / warehouses | `code` unique |
| `users` | People who log in | email unique and lowercase; admin ⇔ no branch |
| `products` | Shared catalogue | `sku` unique; price ≥ 0 |
| `inventory` | **Current** stock, one row per (branch, product) | unique (branch, product); `on_hand ≥ 0`; `0 ≤ reserved ≤ on_hand`; `available` is a generated column |
| `stock_movements` | **History** of every stock change (ledger) | append-only (trigger); reason required; must change something |
| `orders`, `order_items` | Customer orders | quantity > 0; one line per product per order; price snapshotted |
| `transfers`, `transfer_items` | Branch-to-branch stock moves | source ≠ destination; quantity > 0 |
| `audit_logs` | Security & change trail | append-only (trigger) |
| `idempotency_keys` | Safe retries | unique (user, key) |

## How the three stock numbers work

| Column | Meaning |
|---|---|
| `on_hand` | Units physically in the branch |
| `reserved` | Units promised to orders (or approved transfers) but not yet handed over |
| `available` | `on_hand − reserved`: what can still be promised. Computed by PostgreSQL, so it can never be stale. |

Because `reserved ≤ on_hand` is a CHECK constraint, **available can never be negative**,
even if application code has a bug. Concurrency safety is built on top of this
guarantee, not instead of it.

## Ledger rule

Every change to an `inventory` row writes exactly one `stock_movements` row **in the
same transaction**, recording the deltas and the resulting values. So:

- `SUM(on_hand_delta)` over a branch/product's movements equals its current `on_hand` (same for `reserved`).
- The ledger cannot be edited or deleted: a trigger raises an error on `UPDATE`/`DELETE`.
  Mistakes are fixed with a new, compensating movement, as in accounting.

## State machines

Implemented in [`order_service.py`](../app/services/order_service.py) (the `TRANSITIONS` table).
Any other move returns `409 invalid_transition` and changes nothing; repeating a move that already
happened returns the current state unchanged.

```
Orders:     pending ──► reserved ──► confirmed ──► fulfilled
               │            │             │
               └────────────┴─────────────┴──► cancelled   (releases reservation)
```

| Transition | Stock effect (all with a ledger entry referencing the order) |
|---|---|
| order reserve | `reserved += qty` (all lines or none) |
| order fulfil | `on_hand -= qty`, `reserved -= qty` |
| order cancel | `reserved -=` what the ledger says this order holds |
