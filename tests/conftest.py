"""Shared test fixtures.

Integration tests run against a real PostgreSQL database (TEST_DATABASE_URL),
not SQLite, because the things being tested (CHECK constraints, triggers, row
locking) are database behaviour.

The schema is built with the real Alembic migrations, so the migrations are
tested too. Every table is truncated after each test.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    test_database_url: str
    database_url: str | None = None


_env = _TestEnv()  # type: ignore[call-arg]
if _env.database_url and _env.test_database_url == _env.database_url:
    raise RuntimeError("TEST_DATABASE_URL must differ from DATABASE_URL: the test suite wipes its database.")

# Must be set before anything imports app.core.config.
os.environ["DATABASE_URL"] = _env.test_database_url
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-long-enough-for-hs256-0123456789")
os.environ["BCRYPT_ROUNDS"] = "4"

from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Branch, Product, Role, User  # noqa: E402

DEFAULT_PASSWORD = "Passw0rd!"


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _env.test_database_url.replace("%", "%%"))
    # Start from an empty schema, whatever an earlier run (or another branch of the code) left behind.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    yield
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_branch(db: Session) -> Callable[..., Branch]:
    def _make(code: str | None = None, name: str | None = None, is_active: bool = True) -> Branch:
        code = code or f"B{uuid.uuid4().hex[:6].upper()}"
        branch = Branch(code=code, name=name or f"Branch {code}", is_active=is_active)
        db.add(branch)
        db.commit()
        return branch

    return _make


@pytest.fixture
def make_user(db: Session) -> Callable[..., User]:
    def _make(
        role: Role,
        branch: Branch | None = None,
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        is_active: bool = True,
    ) -> User:
        user = User(
            email=email or f"{role.value}.{uuid.uuid4().hex[:8]}@example.com",
            full_name=f"Test {role.value}",
            password_hash=hash_password(password),
            role=role,
            branch_id=branch.id if branch else None,
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        return user

    return _make


def auth_headers(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, user.role.value, user.branch_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(make_branch, make_user) -> dict:
    """Two branches with a manager and a staff member each, plus an admin."""
    kandy, colombo = make_branch("KDY", "Kandy"), make_branch("CMB", "Colombo")
    return {
        "kandy": kandy,
        "colombo": colombo,
        "admin": make_user(Role.ADMIN),
        "kandy_manager": make_user(Role.BRANCH_MANAGER, kandy),
        "kandy_staff": make_user(Role.STAFF, kandy),
        "colombo_manager": make_user(Role.BRANCH_MANAGER, colombo),
        "colombo_staff": make_user(Role.STAFF, colombo),
    }


@pytest.fixture
def make_product(db: Session) -> Callable[..., Product]:
    def _make(sku: str | None = None, price: str = "100.00", is_active: bool = True) -> Product:
        product = Product(
            sku=sku or f"SKU-{uuid.uuid4().hex[:6].upper()}",
            name=f"Product {sku or ''}".strip(),
            unit_price=price,
            is_active=is_active,
        )
        db.add(product)
        db.commit()
        return product

    return _make
