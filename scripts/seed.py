"""Load demo data: three branches and one admin, manager and staff user per branch.

Safe to run repeatedly (existing rows are left alone). All demo users get the
password in SEED_DEFAULT_PASSWORD, so no password is ever stored in the repo.

    python -m scripts.seed
"""

import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Branch, Role, User

BRANCHES = [
    ("KDY", "Kandy Branch", "Peradeniya Road, Kandy"),
    ("CMB", "Colombo Branch", "Galle Road, Colombo 03"),
    ("GLE", "Galle Branch", "Main Street, Galle"),
]


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

        created = 0
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
                created += 1
        db.commit()

    print(f"Seed complete: {len(branches)} branches, {created} new users.")
    for email, _, role, code in users:
        print(f"  {role.value:<15} {code or '-':<4} {email}")


if __name__ == "__main__":
    main()
