from sqlalchemy import select

from app.models import AuditLog, AuditOutcome, Role
from tests.conftest import DEFAULT_PASSWORD, auth_headers

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def test_login_returns_token_and_user(client, make_branch, make_user):
    user = make_user(Role.STAFF, make_branch(), email="nimal@example.com")
    res = client.post(LOGIN, json={"email": "nimal@example.com", "password": DEFAULT_PASSWORD})

    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["id"] == str(user.id)
    assert "password" not in str(body["user"]) and "password_hash" not in body["user"]


def test_login_email_is_case_insensitive(client, make_user):
    make_user(Role.ADMIN, email="admin@example.com")
    res = client.post(LOGIN, json={"email": "ADMIN@Example.com", "password": DEFAULT_PASSWORD})
    assert res.status_code == 200


def test_wrong_password_and_unknown_email_are_indistinguishable(client, make_user):
    make_user(Role.ADMIN, email="admin@example.com")
    wrong_pw = client.post(LOGIN, json={"email": "admin@example.com", "password": "nope"})
    unknown = client.post(LOGIN, json={"email": "ghost@example.com", "password": "nope"})

    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["error"] == unknown.json()["error"]
    assert wrong_pw.json()["error"]["code"] == "invalid_credentials"
    assert wrong_pw.headers["WWW-Authenticate"] == "Bearer"


def test_inactive_user_cannot_log_in(client, make_branch, make_user):
    make_user(Role.STAFF, make_branch(), email="old@example.com", is_active=False)
    res = client.post(LOGIN, json={"email": "old@example.com", "password": DEFAULT_PASSWORD})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


def test_user_of_inactive_branch_cannot_log_in(client, make_branch, make_user):
    make_user(Role.STAFF, make_branch(is_active=False), email="closed@example.com")
    res = client.post(LOGIN, json={"email": "closed@example.com", "password": DEFAULT_PASSWORD})
    assert res.status_code == 401


def test_logins_are_audited(client, db, make_user):
    user = make_user(Role.ADMIN, email="admin@example.com")
    client.post(LOGIN, json={"email": "admin@example.com", "password": "bad"})
    client.post(LOGIN, json={"email": "admin@example.com", "password": DEFAULT_PASSWORD})

    entries = db.scalars(select(AuditLog).where(AuditLog.action == "auth.login").order_by(AuditLog.created_at)).all()
    assert [e.outcome for e in entries] == [AuditOutcome.FAILURE, AuditOutcome.SUCCESS]
    assert entries[0].details["reason"] == "wrong_password"
    assert all(e.actor_id == user.id for e in entries)
    assert all(e.request_id for e in entries)


def test_me_returns_user_and_permissions(client, make_branch, make_user):
    user = make_user(Role.STAFF, make_branch())
    res = client.get(ME, headers=auth_headers(user))
    assert res.status_code == 200
    assert res.json()["email"] == user.email
    assert "order:create" in res.json()["permissions"]
    assert "inventory:adjust" not in res.json()["permissions"]


def test_me_without_token_is_401(client):
    res = client.get(ME)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "missing_token"


def test_me_with_garbage_token_is_401(client):
    res = client.get(ME, headers={"Authorization": "Bearer abc.def.ghi"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_token"


def test_deactivation_revokes_existing_tokens_immediately(client, db, make_branch, make_user):
    user = make_user(Role.STAFF, make_branch())
    headers = auth_headers(user)
    assert client.get(ME, headers=headers).status_code == 200

    user.is_active = False
    db.commit()
    assert client.get(ME, headers=headers).status_code == 401


def test_validation_errors_use_standard_shape(client):
    res = client.post(LOGIN, json={"email": "not-an-email", "password": ""})
    assert res.status_code == 422
    body = res.json()
    assert body["error"]["code"] == "validation_error"
    fields = {d["field"] for d in body["error"]["details"]}
    assert {"body.email", "body.password"} <= fields
    assert body["request_id"]


def test_request_id_is_echoed(client):
    res = client.get(ME, headers={"X-Request-ID": "trace-123"})
    assert res.headers["X-Request-ID"] == "trace-123"
    assert res.json()["request_id"] == "trace-123"
