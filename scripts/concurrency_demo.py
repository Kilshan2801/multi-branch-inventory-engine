"""Live demonstration of the concurrency challenge against a RUNNING server.

    uvicorn app.main:app            # terminal 1
    python -m scripts.concurrency_demo   # terminal 2   (after `python -m scripts.seed`)

It creates a fresh product, stocks exactly 10 units at Kandy, then fires three
create-and-reserve requests for 6, 5 and 4 units at the same instant and
prints who won, who was refused, and the final stock level.

Options:
    --base-url http://127.0.0.1:8000   server address
    --rounds 5                         repeat the race several times
"""

import argparse
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.core.config import get_settings

API = "/api/v1"


def login(client: httpx.Client, email: str, password: str) -> dict[str, str]:
    res = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    res.raise_for_status()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def one_round(base_url: str, admin: dict, staff: dict, kandy_id: str, round_no: int) -> None:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        sku = f"DEMO-{uuid.uuid4().hex[:6].upper()}"
        product = client.post(
            f"{API}/products", headers=admin, json={"sku": sku, "name": "Concurrency demo item", "unit_price": "100.00"}
        ).json()
        client.post(
            f"{API}/branches/{kandy_id}/inventory/adjustments",
            headers=admin,
            json={"product_id": product["id"], "quantity_delta": 10, "reason": "Concurrency demo stock"},
        )

    quantities = [6, 5, 4]
    barrier = threading.Barrier(len(quantities))

    def reserve(quantity: int):
        with httpx.Client(base_url=base_url, timeout=30) as c:
            payload = {
                "branch_id": kandy_id,
                "customer_name": f"Customer wanting {quantity}",
                "reserve": True,
                "items": [{"product_id": product["id"], "quantity": quantity}],
            }
            barrier.wait()  # release all three requests at the same instant
            started = time.perf_counter()
            res = c.post(f"{API}/orders", headers=staff, json=payload)
            return quantity, res, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(len(quantities)) as pool:
        results = list(pool.map(reserve, quantities))

    with httpx.Client(base_url=base_url, timeout=30) as client:
        level = client.get(f"{API}/branches/{kandy_id}/inventory/{product['id']}", headers=admin).json()

    print(f"\nRound {round_no}: {sku} at Kandy, 10 units on hand; requests for 6, 5 and 4 sent simultaneously")
    for quantity, res, ms in sorted(results, key=lambda r: r[0], reverse=True):
        if res.status_code == 201:
            print(f"  {quantity} units  -> 201 RESERVED   order {res.json()['id'][:8]}…  ({ms:.0f} ms)")
        else:
            err = res.json()["error"]
            available = err.get("details", {}).get("shortages", [{}])[0].get("available", "?")
            print(f"  {quantity} units  -> {res.status_code} {err['code']} (only {available} left)  ({ms:.0f} ms)")
    won = sum(q for q, r, _ in results if r.status_code == 201)
    print(
        f"  Result: on_hand={level['on_hand']} reserved={level['reserved']} available={level['available']}"
        f"  (reserved by winners: {won})  {'OK' if level['reserved'] == won <= 10 else 'PROBLEM'}"
    )


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    password = get_settings().seed_default_password
    if not password:
        raise SystemExit("Set SEED_DEFAULT_PASSWORD (the demo users' password) in .env")

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        admin = login(client, "admin@example.com", password)
        staff = login(client, "staff.kdy@example.com", password)
        branches = client.get(f"{API}/branches", headers=admin).json()["items"]
        kandy_id = next(b["id"] for b in branches if b["code"] == "KDY")

    for round_no in range(1, args.rounds + 1):
        one_round(args.base_url, admin, staff, kandy_id, round_no)


if __name__ == "__main__":
    main()
