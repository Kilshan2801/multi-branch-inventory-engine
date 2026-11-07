import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_is_hashed_and_verifiable():
    hashed = hash_password("Passw0rd!")
    assert hashed != "Passw0rd!"
    assert verify_password("Passw0rd!", hashed)
    assert not verify_password("passw0rd!", hashed)


def test_same_password_gets_different_hashes():
    assert hash_password("Passw0rd!") != hash_password("Passw0rd!")


def test_verify_without_hash_is_false():
    assert verify_password("anything", None) is False


def test_token_round_trip():
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, "staff", uuid.uuid4())
    assert decode_access_token(token) == user_id
    assert expires_in == get_settings().access_token_expire_minutes * 60


def _encode(payload: dict, key: str | None = None, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, key or get_settings().jwt_secret_key, algorithm=algorithm)


def _claims(**overrides) -> dict:
    now = datetime.now(UTC)
    claims = {"sub": str(uuid.uuid4()), "type": "access", "iat": now, "exp": now + timedelta(minutes=5)}
    claims.update(overrides)
    return claims


def test_expired_token_rejected():
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(AuthenticationError) as exc:
        decode_access_token(_encode(_claims(iat=past, exp=past + timedelta(minutes=1))))
    assert exc.value.code == "token_expired"


def test_token_signed_with_other_key_rejected():
    with pytest.raises(AuthenticationError) as exc:
        decode_access_token(_encode(_claims(), key="some-other-secret-key-of-sufficient-length-123"))
    assert exc.value.code == "invalid_token"


def test_unsigned_alg_none_token_rejected():
    token = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(AuthenticationError):
        decode_access_token(token)


def test_wrong_token_type_rejected():
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(_claims(type="refresh")))


def test_token_missing_subject_rejected():
    claims = _claims()
    del claims["sub"]
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(claims))


def test_garbage_token_rejected():
    with pytest.raises(AuthenticationError):
        decode_access_token("not-a-jwt")
