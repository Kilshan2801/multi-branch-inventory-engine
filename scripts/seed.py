"""Load demo data: three branches, one admin plus a manager and a staff member per
branch, a few products, and opening stock.

Safe to run repeatedly (existing rows are left alone). All demo users get the
password in SEED_DEFAULT_PASSWORD, so no password is ever stored in the repo.
Opening stock goes through the real adjustment service, so it appears in the
stock ledger and audit log like any other stock change.

    python -m scripts.seed
"""

import sys
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Branch, InventoryItem, Product, Role, User
from app.schemas.inventory import AdjustmentCreate
from app.services.audit import RequestContext
from app.services.inventory_service import adjust_stock

BRANCHES = [
    ("KDY", "Kandy Branch", "Peradeniya Road, Kandy"),
    ("CMB", "Colombo Branch", "Galle Road, Colombo 03"),
    ("GLE", "Galle Branch", "Main Street, Galle"),
]

PRODUCTS = [
    ("PHN-001", "Smartphone X", "128 GB, black", "89999.00"),
    ("TAB-001", "Tablet S", "10-inch, Wi-Fi", "64999.00"),
    ("LAP-001", "Laptop Pro 14", "16 GB RAM, 512 GB SSD", "289999.00"),
    ("HDP-001", "Wireless Headphones", "Noise cancelling", "24999.00"),
]

# Kandy holds exactly 10 phones: the numbers used in the task's concurrency scenario.
OPENING_STOCK = {
    "KDY": {"PHN-001": 10, "TAB-001": 5, "LAP-001": 3, "HDP-001": 20},
    "CMB": {"PHN-001": 4, "TAB-001": 8, "HDP-001": 12},
    "GLE": {"PHN-001": 2, "LAP-001": 1},
}


def main() -> None:
    password = get_settings().seed_default_password
    if not password:
        sys.exit("Set SEED_DEFAULT_PASSWORD (e.g. in .env) before seeding.")

    with SessionLocal() as db:
        branches: dict[str, Branch] = {}
        for code, name, address in BRANCHES:
            branch = db.scalar(select(Branch).where(Branch.code == code))
            if branch is None:
                branch = Branch(code=code, name=name, address=address)
                db.add(branch)
                db.flush()
            branches[code] = branch

        pw_hash = hash_password(password)
        users = [("admin@example.com", "System Administrator", Role.ADMIN, None)]
        for code in branches:
            slug = code.lower()
            users.append((f"manager.{slug}@example.com", f"{code} Branch Manager", Role.BRANCH_MANAGER, code))
            users.append((f"staff.{slug}@example.com", f"{code} Staff Member", Role.STAFF, code))

        new_users = 0
        for email, full_name, role, code in users:
            if db.scalar(select(User).where(User.email == email)) is None:
                db.add(
                    User(
                        email=email,
                        full_name=full_name,
                        password_hash=pw_hash,
                        role=role,
                        branch_id=branches[code].id if code else None,
                    )
                )
                new_users += 1
        db.flush()

        products: dict[str, Product] = {}
        for sku, name, description, price in PRODUCTS:
            product = db.scalar(select(Product).where(Product.sku == sku))
            if product is None:
                product = Product(sku=sku, name=name, description=description, unit_price=Decimal(price))
                db.add(product)
                db.flush()
            products[sku] = product

        admin = db.scalar(select(User).where(User.email == "admin@example.com"))
        stocked = 0
        for code, levels in OPENING_STOCK.items():
            for sku, quantity in levels.items():
                exists = db.scalar(
                    select(InventoryItem).where(
                        InventoryItem.branch_id == branches[code].id, InventoryItem.product_id == products[sku].id
                    )
                )
                if exists is None:
                    adjust_stock(
                        db,
                        branches[code].id,
                        AdjustmentCreate(
                            product_id=products[sku].id,
                            quantity_delta=quantity,
                            reason="Opening stock (seed)",
                        ),
                        admin,
                        RequestContext(request_id="seed"),
                    )
                    stocked += 1
        db.commit()

    print(
        f"Seed complete: {len(branches)} branches, {new_users} new users, "
        f"{len(products)} products, {stocked} new stock lines."
    )
    print("\nUsers (password = SEED_DEFAULT_PASSWORD):")
    for email, _, role, code in users:
        print(f"  {role.value:<15} {code or '-':<4} {email}")
    print("\nOpening stock:")
    for code, levels in OPENING_STOCK.items():
        print(f"  {code}: " + ", ".join(f"{sku} x{qty}" for sku, qty in levels.items()))


if __name__ == "__main__":
    main()
