"""PKCE state storage for the Microsoft OAuth flow.

The /api/v1/auth/microsoft/start route generates a random state token
and a PKCE code_verifier per request and stashes them in Redis keyed
by the state token. The /api/v1/auth/microsoft/callback route then
consumes the state (atomically, so a replayed callback fails) to
recover the firm_id and code_verifier needed to complete the token
exchange.

Atomicity is provided by Redis GETDEL: a single round-trip that
returns the value AND deletes the key, so two concurrent callbacks
with the same state can't both succeed.

Keys are prefixed with `oauth:state:` so they're easy to inspect /
flush in operations and don't collide with other Redis use cases that
land on the same DB.
"""
import json
import secrets
import uuid

from coworker.db.redis import get_redis


_KEY_PREFIX = "oauth:state:"
_TTL_SECONDS = 600  # 10 minutes — generous for human-paced login


class OAuthStateError(Exception):
    """State token missing, expired, or already consumed.

    Errors deliberately do not echo the supplied token in their message,
    so a buggy log line cannot leak a still-valid state to logs.
    """


def _new_state_token() -> str:
    # 32 bytes → 43 url-safe base64 chars; >>2^128 search space.
    return secrets.token_urlsafe(32)


def _new_code_verifier() -> str:
    # PKCE RFC 7636 §4.1: 43–128 characters from the unreserved set.
    # token_urlsafe(64) → 86 chars from [A-Za-z0-9_-], well within range.
    return secrets.token_urlsafe(64)


async def create_state(firm_id: uuid.UUID) -> tuple[str, str]:
    """Create a fresh PKCE state for a sign-in attempt.

    Returns (state_token, code_verifier). The state_token is what we
    put in the OAuth `state` parameter; the code_verifier stays
    server-side and is sent to Microsoft in the token-exchange step
    along with the auth code.
    """
    state_token = _new_state_token()
    code_verifier = _new_code_verifier()
    payload = json.dumps(
        {"firm_id": str(firm_id), "code_verifier": code_verifier}
    )
    client = get_redis()
    await client.set(_KEY_PREFIX + state_token, payload, ex=_TTL_SECONDS)
    return state_token, code_verifier


async def consume_state(state_token: str) -> tuple[uuid.UUID, str]:
    """Atomically pop the stored payload for a state token.

    Returns (firm_id, code_verifier). Raises OAuthStateError if the
    token is missing, expired, or already consumed (replay).
    """
    if not state_token:
        raise OAuthStateError("invalid or expired state")
    client = get_redis()
    raw = await client.getdel(_KEY_PREFIX + state_token)
    if raw is None:
        raise OAuthStateError("invalid or expired state")
    try:
        data = json.loads(raw)
        firm_id = uuid.UUID(data["firm_id"])
        code_verifier = data["code_verifier"]
    except (ValueError, KeyError, TypeError) as exc:
        # Stored payload is corrupt — treat as if the state was invalid.
        # Don't let exception detail leak the stored bytes.
        raise OAuthStateError("invalid or expired state") from exc
    return firm_id, code_verifier
