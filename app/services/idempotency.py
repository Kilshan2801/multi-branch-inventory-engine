"""Idempotency keys: make a retried write request safe.

The problem: a client sends "create order", the network drops the response,
and the client retries. Without protection, the order (and its reservation)
is created twice.

The solution: the client sends a unique `Idempotency-Key` header with the
request and re-sends the SAME key when retrying. The server stores the key
together with the response, and answers a retry with the stored response
instead of doing the work again.

How it stays correct under concurrency (two copies arriving at once):

    BEGIN
      INSERT INTO idempotency_keys (user, key, ...) ON CONFLICT DO NOTHING
      -- the unique index on (user_id, key) makes a second, concurrent INSERT of
      -- the same key WAIT here until the first transaction finishes
      ... do the real work ...
      UPDATE idempotency_keys SET response = ...
    COMMIT

* First copy:  inserts the key, does the work, stores the response, commits.
* Second copy: its INSERT waits; when the first commits, the INSERT finds the
               row and does nothing, so the second copy reads the stored
               response and returns it. The work ran exactly once.
* If the first copy FAILS (e.g. insufficient stock) its whole transaction,
  including the key, rolls back. A retry then runs fresh. Failures are not
  cached, so a retry can succeed once stock has been replenished.

Reusing a key for a different request body is a client bug and gets 422.
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import BusinessValidationError, ConflictError
from app.models import IdempotencyKey, User

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"


@dataclass(frozen=True)
class Outcome:
    status_code: int
    body: Any
    replayed: bool


def request_fingerprint(endpoint: str, payload: Any) -> str:
    canonical = json.dumps({"endpoint": endpoint, "payload": jsonable_encoder(payload)}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def execute(
    db: Session,
    *,
    user: User,
    key: str | None,
    endpoint: str,
    payload: Any,
    status_code: int,
    operation: Callable[[], BaseModel],
) -> Outcome:
    """Run `operation` (which must not commit) and commit, at most once per (user, key)."""
    if key is None:
        body = jsonable_encoder(operation())
        db.commit()
        return Outcome(status_code, body, replayed=False)

    fingerprint = request_fingerprint(endpoint, payload)
    claimed = db.execute(
        pg_insert(IdempotencyKey)
        .values(id=uuid.uuid4(), user_id=user.id, key=key, endpoint=endpoint, request_hash=fingerprint)
        .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
        .returning(IdempotencyKey.id)
    ).scalar_one_or_none()

    if claimed is None:
        stored = db.execute(
            select(IdempotencyKey.request_hash, IdempotencyKey.response_status, IdempotencyKey.response_body).where(
                IdempotencyKey.user_id == user.id, IdempotencyKey.key == key
            )
        ).first()
        db.rollback()  # nothing was written; end the transaction
        if stored is None or stored.response_body is None:  # pragma: no cover - defensive
            raise ConflictError(
                "A request with this Idempotency-Key is still being processed", code="idempotency_in_progress"
            )
        if stored.request_hash != fingerprint:
            raise BusinessValidationError(
                f"This {HEADER} was already used for a different request", code="idempotency_key_reused"
            )
        return Outcome(stored.response_status or status_code, stored.response_body, replayed=True)

    try:
        body = jsonable_encoder(operation())
        db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.id == claimed)
            .values(response_status=status_code, response_body=body, completed_at=func.now())
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Outcome(status_code, body, replayed=False)
