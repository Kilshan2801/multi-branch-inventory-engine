"""Password hashing (bcrypt) and JWT access tokens."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.errors import AuthenticationError

# bcrypt only uses the first 72 bytes of a password; longer input is rejected at validation time.
BCRYPT_MAX_BYTES = 72

# Compared against when a login email doesn't exist, so "unknown user" and
# "wrong password" take the same time and can't be told apart (user enumeration).
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    candidate = password.encode()[:BCRYPT_MAX_BYTES]
    if password_hash is None:
        bcrypt.checkpw(candidate, _DUMMY_HASH.encode())
        return False
    return bcrypt.checkpw(candidate, password_hash.encode())


def create_access_token(user_id: uuid.UUID, role: str, branch_id: uuid.UUID | None) -> tuple[str, int]:
    """Return (token, lifetime_seconds).

    The role/branch claims are informational for clients only: on every request
    the server reloads the user from the database, so a role change or
    deactivation takes effect immediately instead of when the token expires.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "branch_id": str(branch_id) if branch_id else None,
        "type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> uuid.UUID:
    """Validate signature, expiry and token type; return the user id."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],  # pinned: never trust the token's own "alg"
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid access token", code="invalid_token") from exc

    if payload.get("type") != "access":
        raise AuthenticationError("Invalid access token", code="invalid_token")
    try:
        return uuid.UUID(payload["sub"])
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("Invalid access token", code="invalid_token") from exc
