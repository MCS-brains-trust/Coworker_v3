"""Session JWT: issue and decode the cookie minted at /api/v1/auth/microsoft/callback.

HS256 signed via SESSION_JWT_SECRET (a settings field, separate from
MASTER_ENCRYPTION_KEY because the threat model and rotation cadence
differ — see config.py for the rationale).

Standard claims only (sub, iat, exp) plus a firm_id custom claim
because every authenticated request needs to set firm_context to that
firm before any DB work. A future Depends(current_user) (Phase 3+)
will decode the cookie, look up the user, and enter firm_context for
the duration of the request.
"""
import datetime as _dt
import uuid
from typing import Any

import jwt

from coworker.config import get_settings


_ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h


def issue_session_jwt(
    *,
    user_id: uuid.UUID,
    firm_id: uuid.UUID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "firm_id": str(firm_id),
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    secret = get_settings().SESSION_JWT_SECRET.get_secret_value()
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def decode_session_jwt(token: str) -> dict[str, Any]:
    secret = get_settings().SESSION_JWT_SECRET.get_secret_value()
    return jwt.decode(token, secret, algorithms=[_ALGORITHM])
