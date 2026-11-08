# Multi-Branch Inventory Engine

This is the backend for a business that runs several shops (branches). Each branch has
its own stock, takes customer orders, reserves items for customers, and can send stock
to other branches.

The main thing I care about in this project is that the stock numbers are always right,
even when lots of requests arrive at the same time, when a request is retried, when
something fails halfway, or when someone tries to do something they're not allowed to.

Built with Python, FastAPI and PostgreSQL. Database changes are handled with Alembic,
and tests are written with Pytest.

## Where things are so far

Right now the project has:

- the full database design, with rules inside the database that stop bad stock numbers
- login with JWT tokens
- three roles (Admin, Branch Manager, Staff), and checks that keep people inside their own branch
- user management for admins
- one consistent error format for every endpoint
- branches and products: create, update, list and deactivate (nothing is ever deleted)
- stock per branch: on hand, reserved and available
- manual stock adjustments with a required reason (managers only)
- a stock history (ledger) that records every change and can't be edited
- an audit log that admins and managers can read

More detail lives in the `docs` folder:

- [docs/SCHEMA.md](docs/SCHEMA.md): the database diagram and the rules on each table
- [docs/ROLES.md](docs/ROLES.md): what each role can and can't do, and why
- [docs/DECISIONS.md](docs/DECISIONS.md): the design choices I made and their trade-offs

## What you need

- Python 3.11 or newer
- PostgreSQL 14 or newer (I used PostgreSQL 18 on Windows). Docker works too.
- Git

## Getting it running

**1. Get the code and install the libraries**

```bash
git clone https://github.com/Kilshan2801/multi-branch-inventory-engine.git
cd multi-branch-inventory-engine
python -m venv .venv
.venv\Scripts\activate          # on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

**2. Create the databases**

You need two databases: one for the app, and a separate one that the tests can wipe freely.
In `psql -U postgres`:

```sql
CREATE USER inventory WITH PASSWORD 'pick-a-password';
CREATE DATABASE inventory OWNER inventory;
CREATE DATABASE inventory_test OWNER inventory;
```

If you'd rather use Docker, `docker compose up -d` creates both for you.

**3. Add your settings**

Copy the example file and fill it in:

```bash
copy .env.example .env          # on macOS/Linux: cp .env.example .env
```

These are the settings that matter:

| Setting | What it's for |
|---|---|
| `DATABASE_URL` | how the app reaches its database, e.g. `postgresql+psycopg://inventory:pick-a-password@localhost:5432/inventory` |
| `TEST_DATABASE_URL` | the same, but for `inventory_test`. Never point this at real data, because the tests empty it. |
| `JWT_SECRET_KEY` | the secret used to sign login tokens, at least 32 characters. Make one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SEED_DEFAULT_PASSWORD` | the password given to the demo users |

Optional: `ACCESS_TOKEN_EXPIRE_MINUTES` (default 30), `JWT_ALGORITHM` (default HS256),
`BCRYPT_ROUNDS` (default 12), `LOG_LEVEL` (default INFO), and the `POSTGRES_*` values, which only Docker uses.

`.env` is never committed. The app won't even start without a database URL and a proper secret key,
so there are no hidden default passwords in the code.

**4. Create the tables and some demo data**

```bash
alembic upgrade head
python -m scripts.seed
```

The seed script adds three branches (Kandy, Colombo, Galle), four products with some opening stock,
and these users. All users share the password from `SEED_DEFAULT_PASSWORD`, and running the script
again is safe:

| User | Role | Branch |
|---|---|---|
| admin@example.com | Admin | all of them |
| manager.kdy@example.com / staff.kdy@example.com | Manager / Staff | Kandy |
| manager.cmb@example.com / staff.cmb@example.com | Manager / Staff | Colombo |
| manager.gle@example.com / staff.gle@example.com | Manager / Staff | Galle |

**5. Start the server**

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs. Log in with `POST /api/v1/auth/login`, copy the
`access_token`, click **Authorize** and paste it. After that you can try every endpoint from the browser.

## Running the tests

```bash
pytest
```

The tests use a real PostgreSQL database (`TEST_DATABASE_URL`), not a fake one, because a lot of
what they check (the rules inside the database, and row locking) only exists in PostgreSQL. Before the tests run,
the test database is rebuilt from the migrations, so the migrations get tested too.

You can also run just one part: `pytest tests/unit` (quick, no database needed) or `pytest tests/integration`.

## Database migrations

```bash
alembic upgrade head        # bring the database up to date
alembic downgrade -1        # undo the last change
```

There are two migrations so far: `0001` creates all the tables, rules and triggers, and `0002` makes the
timestamps on the stock history and audit log record the exact moment of each change.

## Endpoints so far

| Endpoint | Who can use it |
|---|---|
| `POST /api/v1/auth/login` | anyone |
| `GET /api/v1/auth/me` | anyone who is logged in |
| `POST /api/v1/users` | admins |
| `GET /api/v1/users` | admins (everyone), managers (their own branch) |
| `GET /api/v1/users/{id}` | admins, managers (their own branch) |
| `PATCH /api/v1/users/{id}` | admins |
| `POST /api/v1/branches` | admins |
| `GET /api/v1/branches`, `GET /api/v1/branches/{id}` | anyone who is logged in |
| `PATCH /api/v1/branches/{id}` | admins (update or deactivate) |
| `POST /api/v1/products` | admins |
| `GET /api/v1/products`, `GET /api/v1/products/{id}` | anyone who is logged in |
| `PATCH /api/v1/products/{id}` | admins (update or deactivate) |
| `GET /api/v1/branches/{id}/inventory` | people in that branch (admins: any branch) |
| `GET /api/v1/branches/{id}/inventory/{product_id}` | people in that branch (admins: any branch) |
| `POST /api/v1/branches/{id}/inventory/adjustments` | that branch's manager, admins |
| `GET /api/v1/branches/{id}/stock-movements` | people in that branch (admins: any branch) |
| `GET /api/v1/audit-logs` | admins (everything), managers (their own branch) |
| `GET /health` | anyone |

When something goes wrong, every endpoint answers in the same format, so a client only has to handle one shape:

```json
{
  "error": { "code": "branch_access_denied", "message": "You do not have access to this branch", "details": null },
  "request_id": "6d14b702099f4fdf903f8f98eb8cf999"
}
```

## How the code is organised

```
app/
  api/        the HTTP endpoints, kept thin: read the request, call a service, send the answer
  core/       settings, database connection, errors, password hashing, tokens, role permissions
  models/     the database tables, with their rules
  schemas/    what requests and responses look like (and their validation)
  services/   the actual business logic
              stock.py is the one place that changes stock numbers (lock, check, change, record)
alembic/      database migrations
scripts/      the demo data script
tests/        unit tests and tests against the real database
docs/         database diagram, roles, design decisions
```
