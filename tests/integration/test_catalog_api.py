"""Branches and products: CRUD, soft delete, validation and permissions."""

import uuid

from app.models import Order, OrderStatus
from tests.conftest import auth_headers

BRANCHES = "/api/v1/branches"
PRODUCTS = "/api/v1/products"


# --- Products ---------------------------------------------------------------


def test_admin_creates_product_with_normalised_sku(client, world):
    res = client.post(
        PRODUCTS,
        json={"sku": " phn-001 ", "name": "Phone", "unit_price": "89999.50"},
        headers=auth_headers(world["admin"]),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["sku"] == "PHN-001"
    assert body["unit_price"] == "89999.50"
    assert body["is_active"] is True
    uuid.UUID(body["id"])


def test_duplicate_sku_is_409(client, world, make_product):
    make_product("PHN-001")
    res = client.post(
        PRODUCTS, json={"sku": "phn-001", "name": "Again", "unit_price": "1"}, headers=auth_headers(world["admin"])
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "sku_taken"


def test_product_validation(client, world):
    res = client.post(
        PRODUCTS, json={"sku": "bad sku!", "name": "", "unit_price": "-1"}, headers=auth_headers(world["admin"])
    )
    assert res.status_code == 422
    fields = {d["field"] for d in res.json()["error"]["details"]}
    assert {"body.sku", "body.name", "body.unit_price"} <= fields


def test_price_with_too_many_decimals_rejected(client, world):
    res = client.post(
        PRODUCTS, json={"sku": "X1", "name": "X", "unit_price": "1.005"}, headers=auth_headers(world["admin"])
    )
    assert res.status_code == 422


def test_manager_cannot_create_product(client, world):
    res = client.post(
        PRODUCTS, json={"sku": "X1", "name": "X", "unit_price": "1"}, headers=auth_headers(world["kandy_manager"])
    )
    assert res.status_code == 403


def test_everyone_can_list_and_search_products(client, world, make_product):
    make_product("PHN-001")
    make_product("TAB-001")
    make_product("PHN-OLD", is_active=False)
    headers = auth_headers(world["kandy_staff"])

    assert client.get(PRODUCTS, headers=headers).json()["total"] == 3
    assert client.get(PRODUCTS, params={"search": "phn"}, headers=headers).json()["total"] == 2
    active = client.get(PRODUCTS, params={"is_active": True}, headers=headers).json()
    assert [p["sku"] for p in active["items"]] == ["PHN-001", "TAB-001"]


def test_pagination(client, world, make_product):
    for i in range(5):
        make_product(f"P-{i}")
    page = client.get(PRODUCTS, params={"limit": 2, "offset": 2}, headers=auth_headers(world["admin"])).json()
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 2
    assert [p["sku"] for p in page["items"]] == ["P-2", "P-3"]


def test_update_and_deactivate_product(client, world, make_product):
    product = make_product("PHN-001")
    headers = auth_headers(world["admin"])
    res = client.patch(f"{PRODUCTS}/{product.id}", json={"unit_price": "120.00", "is_active": False}, headers=headers)
    assert res.status_code == 200
    assert res.json()["unit_price"] == "120.00" and res.json()["is_active"] is False
    # Soft delete: still readable by id.
    assert client.get(f"{PRODUCTS}/{product.id}", headers=headers).status_code == 200


def test_empty_update_rejected(client, world, make_product):
    product = make_product()
    res = client.patch(f"{PRODUCTS}/{product.id}", json={}, headers=auth_headers(world["admin"]))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "empty_update"


def test_unknown_product_404(client, world):
    res = client.get(f"{PRODUCTS}/{uuid.uuid4()}", headers=auth_headers(world["admin"]))
    assert res.status_code == 404


def test_malformed_id_is_422(client, world):
    res = client.get(f"{PRODUCTS}/not-a-uuid", headers=auth_headers(world["admin"]))
    assert res.status_code == 422


# --- Branches ---------------------------------------------------------------


def test_admin_creates_branch(client, world):
    res = client.post(BRANCHES, json={"code": "ngb", "name": "Negombo"}, headers=auth_headers(world["admin"]))
    assert res.status_code == 201
    assert res.json()["code"] == "NGB"


def test_duplicate_branch_code_409(client, world):
    res = client.post(BRANCHES, json={"code": "KDY", "name": "Another Kandy"}, headers=auth_headers(world["admin"]))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "branch_code_taken"


def test_staff_cannot_create_branch(client, world):
    res = client.post(BRANCHES, json={"code": "NGB", "name": "Negombo"}, headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 403


def test_staff_can_see_branch_directory(client, world):
    res = client.get(BRANCHES, headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 200
    assert [b["code"] for b in res.json()["items"]] == ["CMB", "KDY"]


def test_deactivate_empty_branch(client, world, make_branch):
    branch = make_branch("NGB")
    res = client.patch(f"{BRANCHES}/{branch.id}", json={"is_active": False}, headers=auth_headers(world["admin"]))
    assert res.status_code == 200 and res.json()["is_active"] is False


def test_cannot_deactivate_branch_with_open_orders(client, db, world):
    db.add(
        Order(branch_id=world["kandy"].id, customer_name="A", created_by=world["admin"].id, status=OrderStatus.PENDING)
    )
    db.commit()
    res = client.patch(
        f"{BRANCHES}/{world['kandy'].id}", json={"is_active": False}, headers=auth_headers(world["admin"])
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "branch_has_open_work"
    assert res.json()["error"]["details"]["open_orders"] == 1
