"""FastAPI dependencies that resolve the authenticated caller.

`current_user` reads the `coworker_session` JWT cookie issued by the
OAuth callback, decodes it, and looks the user up under
`firm_context(firm_id)` so the SELECT is RLS-scoped to the JWT's
claimed firm. Every failure path returns the same generic 401 — same
status, same body — so the response does not distinguish "no cookie"
from "expired token" from "forged signature" from "unknown user".
An attacker probing the endpoint cannot tell which condition fired.

Cross-firm resistance falls out of RLS. A forged JWT that flips the
`firm_id` claim (which would require the signing secret) and pairs it
with a `sub` that belongs to a different firm still won't return a
user row: the SELECT runs under `firm_context(claimed_firm_id)` and
RLS filters out users in any other firm. The lookup yields None and
the dependency raises 401. So even with a leaked secret, an attacker
cannot impersonate a user in a firm they don't already belong to —
they can only impersonate a user inside the firm whose identity their
forgery already implies.

The `firm_context` block exits before the dependency returns. The
firm scope is re-established later by downstream dependencies
(notably `graph_context`) for the duration of their own DB work.
Holding it across the whole request would over-scope it to code paths
that legitimately don't need it.
"""
import uuid
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.models.tenancy import User
from coworker.db.session import firm_context, get_session
from coworker.security.session import decode_session_jwt


_SESSION_COOKIE_NAME = "coworker_session"
_GENERIC_AUTH_REQUIRED_DETAIL = "authentication required"


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_GENERIC_AUTH_REQUIRED_DETAIL,
    )


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the authenticated user from the session cookie or raise 401.

    Failure cases that all collapse to the same 401 response:
      - no `coworker_session` cookie on the request
      - cookie present but JWT decode fails (any `jwt.PyJWTError`:
        bad signature, expired, malformed, etc.)
      - decoded claims missing `sub` or `firm_id`, or either is not a
        valid UUID string
      - claims are well-formed but no user row matches the `sub`
        within `firm_context(firm_id)` (user deleted, or sub belongs
        to a user in a different firm than the claim states)
    """
    token = request.cookies.get(_SESSION_COOKIE_NAME)
    if not token:
        raise _unauthenticated()

    try:
        claims: dict[str, Any] = decode_session_jwt(token)
    except jwt.PyJWTError:
        raise _unauthenticated()

    try:
        user_id = uuid.UUID(claims["sub"])
        firm_id = uuid.UUID(claims["firm_id"])
    except (KeyError, ValueError, TypeError):
        raise _unauthenticated()

    async with firm_context(firm_id):
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()

    if user is None:
        raise _unauthenticated()
    return user
