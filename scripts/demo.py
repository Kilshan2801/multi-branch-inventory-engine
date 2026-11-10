"""Walk through the four scenarios the live demo must show, against a RUNNING server.

    python -m scripts.seed        # once
    uvicorn app.main:app          # terminal 1
    python -m scripts.demo        # terminal 2

Scenarios:
    1. A successful order        (create + reserve → confirm → fulfil)
    2. A rejected over-reservation (409, nothing changes)
    3. A cancellation            (reserved stock is released exactly)
    4. A transfer                (request → approve → dispatch → receive, stock conserved)
    +  A branch-boundary check   (staff of one branch can't read another's stock)

Run one scenario with e.g. `python -m scripts.demo --only 4`.
"""

import argparse
import json
import os
import sys

import httpx

from app.core.config import get_settings

API = "/api/v1"

# Colours only on a real terminal (and enable them on Windows consoles).
if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
    if os.name == "nt":
        os.system("")  # switches the Windows console into ANSI mode
    BOLD, GREEN, RED, DIM, RESET = "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"
else:
    BOLD = GREEN = RED = DIM = RESET = ""


class Demo:
    def __init__(self, base_url: str, password: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=30)
        self.tokens = {}
        for who, email in {
            "admin": "admin@example.com",
            "kdy_staff": "staff.kdy@example.com",
            "kdy_manager": "manager.kdy@example.com",
            "cmb_staff": "staff.cmb@example.com",
            "cmb_manager": "manager.cmb@example.com",
        }.items():
            res = self.http.post(f"{API}/auth/login", json={"email": email, "password": password})
            res.raise_for_status()
            self.tokens[who] = res.json()["access_token"]

        branches = self.call("admin", "GET", "/branches", show=False).json()["items"]
        self.branch = {b["code"]: b["id"] for b in branches}
        products = self.call("admin", "GET", "/products", show=False).json()["items"]
        self.product = {p["sku"]: p["id"] for p in products}

    def call(self, who: str, method: str, path: str, body=None, *, show: bool = True, expect: int | None = None):
        res = self.http.request(
            method, f"{API}{path}", json=body, headers={"Authorization": f"Bearer {self.tokens[who]}"}
        )
        if show:
            ok = expect is None or res.status_code == expect
            colour = GREEN if res.status_code < 400 else RED
            print(f"  {DIM}{who:<12}{RESET} {method} {path}")
            print(f"  {' ' * 12} → {colour}{res.status_code}{RESET}" + ("" if ok else f"  (expected {expect}!)"))
        return res

    def stock(self, code: str, sku: str) -> str:
        res = self.call("admin", "GET", f"/branches/{self.branch[code]}/inventory/{self.product[sku]}", show=False)
        if res.status_code == 404:
            return f"{code} {sku}: on_hand=0 reserved=0 available=0"
        s = res.json()
        return f"{code} {sku}: on_hand={s['on_hand']} reserved={s['reserved']} available={s['available']}"

    def show_stock(self, *pairs: tuple[str, str]) -> None:
        for code, sku in pairs:
            print(f"    {BOLD}{self.stock(code, sku)}{RESET}")

    def order_payload(self, quantity: int, reserve: bool = True) -> dict:
        return {
            "branch_id": self.branch["KDY"],
            "customer_name": "Demo Customer",
            "reserve": reserve,
            "items": [{"product_id": self.product["PHN-001"], "quantity": quantity}],
        }


def heading(title: str) -> None:
    print(f"\n{BOLD}{'═' * 72}\n {title}\n{'═' * 72}{RESET}")


def scenario_successful_order(d: Demo) -> None:
    heading("1. Successful order: create + reserve → confirm → fulfil")
    d.show_stock(("KDY", "PHN-001"))
    order = d.call("kdy_staff", "POST", "/orders", d.order_payload(2), expect=201).json()
    print(f"    order {order['id'][:8]}… status={order['status']} total={order['total_amount']}")
    d.show_stock(("KDY", "PHN-001"))
    d.call("kdy_staff", "POST", f"/orders/{order['id']}/confirm", expect=200)
    done = d.call("kdy_staff", "POST", f"/orders/{order['id']}/fulfill", expect=200).json()
    print(f"    status={done['status']}  (2 phones left the branch)")
    d.show_stock(("KDY", "PHN-001"))


def scenario_rejected(d: Demo) -> None:
    heading("2. Rejected over-reservation: ask for more than is available")
    d.show_stock(("KDY", "PHN-001"))
    res = d.call("kdy_staff", "POST", "/orders", d.order_payload(999), expect=409)
    print(f"    {json.dumps(res.json()['error'], indent=2)}".replace("\n", "\n    "))
    print("    → no order was created, nothing was reserved:")
    d.show_stock(("KDY", "PHN-001"))


def scenario_cancellation(d: Demo) -> None:
    heading("3. Cancellation: reserved stock is released exactly")
    d.show_stock(("KDY", "PHN-001"))
    order = d.call("kdy_staff", "POST", "/orders", d.order_payload(3), expect=201).json()
    d.show_stock(("KDY", "PHN-001"))
    res = d.call(
        "kdy_staff", "POST", f"/orders/{order['id']}/cancel", {"reason": "Customer changed mind"}, expect=200
    ).json()
    print(f"    status={res['status']} reason={res['cancellation_reason']!r}")
    d.show_stock(("KDY", "PHN-001"))
    print("    Cancelling again is harmless (idempotent):")
    d.call("kdy_staff", "POST", f"/orders/{order['id']}/cancel", expect=200)
    d.show_stock(("KDY", "PHN-001"))


def scenario_transfer(d: Demo) -> None:
    heading("4. Transfer: Kandy → Colombo, 3 tablets")
    pairs = (("KDY", "TAB-001"), ("CMB", "TAB-001"))
    d.show_stock(*pairs)
    body = {
        "source_branch_id": d.branch["KDY"],
        "destination_branch_id": d.branch["CMB"],
        "items": [{"product_id": d.product["TAB-001"], "quantity": 3}],
        "notes": "Demo transfer",
    }
    t = d.call("cmb_manager", "POST", "/transfers", body, expect=201).json()
    print(f"    transfer {t['id'][:8]}… status={t['status']}")

    print("    Colombo can't approve its own request (only the source branch can):")
    d.call("cmb_manager", "POST", f"/transfers/{t['id']}/approve", expect=403)

    d.call("kdy_manager", "POST", f"/transfers/{t['id']}/approve", expect=200)
    print("    approved → 3 tablets reserved at Kandy:")
    d.show_stock(*pairs)
    d.call("kdy_manager", "POST", f"/transfers/{t['id']}/dispatch", expect=200)
    print("    dispatched → left Kandy, in transit, not yet in Colombo:")
    d.show_stock(*pairs)
    d.call("cmb_staff", "POST", f"/transfers/{t['id']}/receive", expect=200)
    print("    received → arrived in Colombo:")
    d.show_stock(*pairs)


def scenario_boundary(d: Demo) -> None:
    heading("+ Branch boundary: Colombo staff try to read Kandy's stock")
    res = d.call("cmb_staff", "GET", f"/branches/{d.branch['KDY']}/inventory", expect=403)
    print(f"    {res.json()['error']}")


SCENARIOS = {
    "1": scenario_successful_order,
    "2": scenario_rejected,
    "3": scenario_cancellation,
    "4": scenario_transfer,
    "5": scenario_boundary,
}


def main() -> None:
    sys.stdout.reconfigure(errors="replace")  # never crash on a console that can't show a symbol
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--only", choices=SCENARIOS, help="Run a single scenario")
    args = parser.parse_args()

    password = get_settings().seed_default_password
    if not password:
        raise SystemExit("Set SEED_DEFAULT_PASSWORD (the demo users' password) in .env")

    demo = Demo(args.base_url, password)
    for key, scenario in SCENARIOS.items():
        if args.only in (None, key):
            scenario(demo)
    print()


if __name__ == "__main__":
    main()
