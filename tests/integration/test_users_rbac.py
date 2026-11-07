"""Role permissions and branch boundaries, exercised through the users API."""

import uuid

from sqlalchemy import select

from app.models import AuditLog, AuditOutcome, User
from tests.conftest import auth_headers

USERS = "/api/v1/users"


def _new_user(branch_id=None, role="staff", email="new.staff@example.com"):
    return {
        "email": email,
        "full_name": "New Person",
        "password": "Str0ngPass!",
        "role": role,
        "branch_id": str(branch_id) if branch_id else None,
    }


# --- Permission (what a role may do) ----------------------------------------


def test_admin_can_create_user(client, db, world):
    res = client.post(USERS, json=_new_user(world["kandy"].id), headers=auth_headers(world["admin"]))
    assert res.status_code == 201, res.text
    created = db.get(User, uuid.UUID(res.json()["id"]))
    assert created.password_hash != "Str0ngPass!"

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "user.create"))
    assert audit.actor_id == world["admin"].id and audit.entity_id == str(created.id)


def test_staff_cannot_create_user_and_attempt_is_audited(client, db, world):
    res = client.post(USERS, json=_new_user(world["kandy"].id), headers=auth_headers(world["kandy_staff"]))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "permission_denied"
    assert db.scalar(select(User).where(User.email == "new.staff@example.com")) is None

    denial = db.scalar(select(AuditLog).where(AuditLog.action == "access.denied"))
    assert denial.outcome == AuditOutcome.DENIED and denial.actor_id == world["kandy_staff"].id


def test_branch_manager_cannot_create_user(client, world):
    res = client.post(USERS, json=_new_user(world["kandy"].id), headers=auth_headers(world["kandy_manager"]))
    assert res.status_code == 403


def test_staff_cannot_list_users(client, world):
    assert client.get(USERS, headers=auth_headers(world["kandy_staff"])).status_code == 403


def test_unauthenticated_request_is_401(client):
    assert client.get(USERS).status_code == 401


# --- Branch scope (which branch a user may touch) ---------------------------


def test_manager_lists_only_own_branch(client, world):
    res = client.get(USERS, headers=auth_headers(world["kandy_manager"]))
    assert res.status_code == 200
    branch_ids = {u["branch_id"] for u in res.json()["items"]}
    assert branch_ids == {str(world["kandy"].id)}
    assert res.json()["total"] == 2


def test_manager_cannot_list_other_branch(client, world):
    res = client.get(
        USERS, params={"branch_id": str(world["colombo"].id)}, headers=auth_headers(world["kandy_manager"])
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "branch_access_denied"


def test_manager_cannot_read_user_of_other_branch(client, db, world):
    res = client.get(f"{USERS}/{world['colombo_staff'].id}", headers=auth_headers(world["kandy_manager"]))
    assert res.status_code == 403
    denial = db.scalar(select(AuditLog).where(AuditLog.action == "access.denied"))
    assert denial.details["reason"] == "branch_scope"


def test_manager_can_read_user_of_own_branch(client, world):
    res = client.get(f"{USERS}/{world['kandy_staff'].id}", headers=auth_headers(world["kandy_manager"]))
    assert res.status_code == 200


def test_admin_sees_all_branches(client, world):
    res = client.get(USERS, headers=auth_headers(world["admin"]))
    assert res.json()["total"] == 5


# --- Validation and conflicts -----------------------------------------------


def test_duplicate_email_is_409(client, world):
    payload = _new_user(world["kandy"].id, email=world["kandy_staff"].email)
    res = client.post(USERS, json=payload, headers=auth_headers(world["admin"]))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "email_taken"


def test_staff_requires_branch(client, world):
    res = client.post(USERS, json=_new_user(None), headers=auth_headers(world["admin"]))
    assert res.status_code == 422


def test_admin_must_not_have_branch(client, world):
    res = client.post(USERS, json=_new_user(world["kandy"].id, role="admin"), headers=auth_headers(world["admin"]))
    assert res.status_code == 422


def test_unknown_branch_rejected(client, world):
    res = client.post(USERS, json=_new_user(uuid.uuid4()), headers=auth_headers(world["admin"]))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "branch_not_found"


def test_weak_password_rejected(client, world):
    payload = _new_user(world["kandy"].id) | {"password": "short"}
    res = client.post(USERS, json=payload, headers=auth_headers(world["admin"]))
    assert res.status_code == 422


def test_unknown_user_is_404(client, world):
    res = client.get(f"{USERS}/{uuid.uuid4()}", headers=auth_headers(world["admin"]))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


# --- Updates ----------------------------------------------------------------


def test_admin_can_deactivate_user(client, world):
    res = client.patch(
        f"{USERS}/{world['kandy_staff'].id}", json={"is_active": False}, headers=auth_headers(world["admin"])
    )
    assert res.status_code == 200 and res.json()["is_active"] is False


def test_admin_cannot_lock_themselves_out(client, world):
    admin = world["admin"]
    res = client.patch(f"{USERS}/{admin.id}", json={"is_active": False}, headers=auth_headers(admin))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "self_lockout"


def test_moving_user_to_another_branch(client, world):
    res = client.patch(
        f"{USERS}/{world['kandy_staff'].id}",
        json={"branch_id": str(world["colombo"].id)},
        headers=auth_headers(world["admin"]),
    )
    assert res.status_code == 200 and res.json()["branch_id"] == str(world["colombo"].id)


def test_demoting_admin_requires_branch(client, world, make_user):
    from app.models import Role

    other_admin = make_user(Role.ADMIN)
    res = client.patch(f"{USERS}/{other_admin.id}", json={"role": "staff"}, headers=auth_headers(world["admin"]))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_role_branch"
