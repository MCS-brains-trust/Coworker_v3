"""Microsoft Graph token-refresh helper.

`refresh_access_token` exchanges a stored refresh_token for a fresh
access_token (and possibly a rotated refresh_token), persists the
rotation, updates the expiry on the user row, audits success or
failure under the user's firm, and returns the new access_token in
plaintext for the caller to use immediately.

Caller invariants
-----------------
1. The session has `firm_context(firm.id)` already entered.
   Both the audit append (NOT NULL firm_id, RLS-scoped INSERT) and
   the user-row UPDATE depend on it. Setting the GUC inside this
   helper would over-scope the firm context — the caller may already
   be composing a wider transaction under that firm.
2. `user.firm_id == firm.id`. Mismatch is a programmer error
   (incorrect dependency wiring) and raises RuntimeError, not a
   recoverable exception. Allowing it to proceed would silently
   encrypt the new tokens under the wrong AAD and corrupt the row.

Failure taxonomy
----------------
Each failure path is audit-logged with `graph.token_refresh_failed`
and re-raised as a typed exception:

    microsoft_4xx        → ConnectorAuthError
    microsoft_5xx        → ConnectorTransient
    network_error        → ConnectorTransient
    corrupt_ciphertext   → ConnectorAuthError

The corrupt_ciphertext branch (InvalidTag from `decrypt_str`) is
covered by code only — producing it deterministically requires
bypassing the encryption helper, so the test for this path is
deferred to Phase 4 fixtures (per Step 0 calibration).

Commit semantics
----------------
- On success: helper FLUSHES; the caller commits, batching the
  user-row update and the success audit into one transaction.
- On failure: helper COMMITS the failure audit before raising. The
  caller receives the exception and its session may be discarded
  (e.g. by FastAPI exception propagation) — committing inside the
  helper guarantees the failure audit lands either way.
"""
import datetime as _dt

import httpx
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import ConnectorAuthError, ConnectorTransient
from coworker.db.models.tenancy import Firm, User
from coworker.security.audit import append_audit
from coworker.security.auth import GRAPH_SCOPES
from coworker.security.encryption import decrypt_str, encrypt_str

_TOKEN_ENDPOINT_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)


async def refresh_access_token(
    session: AsyncSession,
    user: User,
    firm: Firm,
) -> str:
    """Refresh `user`'s Microsoft access token; return the new plaintext token.

    Updates `user.ms_access_token_ciphertext`, `user.ms_token_expires_at`,
    and (if Microsoft rotated it) `user.ms_refresh_token_ciphertext`.
    Appends a `graph.token_refreshed` audit entry. Caller commits.

    Raises:
        RuntimeError: `user.firm_id != firm.id` (programmer error).
        ConnectorAuthError: Microsoft rejected the refresh (4xx),
            or stored ciphertext could not be decrypted.
        ConnectorTransient: Microsoft 5xx or network failure.
    """
    if user.firm_id != firm.id:
        raise RuntimeError(
            "refresh_access_token called with mismatched user.firm_id "
            f"({user.firm_id}) and firm.id ({firm.id}) — programmer error, "
            "the dependency wiring is wrong"
        )

    firm_id_str = str(firm.id)
    user_id_str = str(user.id)

    # NULL ciphertexts shouldn't reach this path (the OAuth callback
    # writes both, and refresh is only invoked for already-onboarded
    # users), but the columns are nullable so we narrow defensively.
    # An obscure AttributeError otherwise; ConnectorAuthError lets the
    # caller surface "sign in again" cleanly.
    if user.ms_refresh_token_ciphertext is None:
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "missing_refresh_token"
        )
        raise ConnectorAuthError(
            "user has no stored refresh token; sign in again"
        )
    if firm.azure_client_secret_ciphertext is None:
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "missing_firm_secret"
        )
        raise ConnectorAuthError(
            "firm Azure client secret is not configured"
        )

    try:
        refresh_token = decrypt_str(
            user.ms_refresh_token_ciphertext, firm_id=firm_id_str
        )
        client_secret = decrypt_str(
            firm.azure_client_secret_ciphertext, firm_id=firm_id_str
        )
    except InvalidTag:
        # Unreachable from real wiring (current_user + graph_context
        # enforce firm-scoping, so AAD will always match). Covered by
        # code today; deterministic test deferred to Phase 4 fixtures.
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "corrupt_ciphertext"
        )
        raise ConnectorAuthError("token ciphertext could not be decrypted")

    token_url = _TOKEN_ENDPOINT_TEMPLATE.format(tenant=firm.azure_tenant_id)
    data = {
        "client_id": firm.azure_client_id,
        "client_secret": client_secret,
        "scope": " ".join(GRAPH_SCOPES),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(token_url, data=data)
    except httpx.RequestError:
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "network_error"
        )
        raise ConnectorTransient(
            "network error talking to Microsoft token endpoint"
        )

    if 500 <= response.status_code < 600:
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "microsoft_5xx"
        )
        raise ConnectorTransient(
            f"Microsoft token endpoint returned {response.status_code}"
        )
    if response.status_code >= 400:
        await _audit_failure_and_commit(
            session, firm_id_str, user_id_str, "microsoft_4xx"
        )
        raise ConnectorAuthError(
            f"Microsoft rejected refresh: HTTP {response.status_code}"
        )

    body = response.json()
    # response.json() returns Any; Microsoft's contract guarantees
    # `access_token` is a string and `expires_in` is an integer-shaped
    # number. str() / int() coerce + narrow for mypy.
    new_access_token: str = str(body["access_token"])
    expires_in = int(body["expires_in"])
    expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
        seconds=expires_in
    )

    # Microsoft may rotate the refresh token. If they do, persist the
    # new one. If they don't, leave the existing ciphertext alone —
    # the old refresh token remains valid until it expires naturally
    # or is revoked tenant-side.
    new_refresh_token = body.get("refresh_token")
    if new_refresh_token:
        user.ms_refresh_token_ciphertext = encrypt_str(
            new_refresh_token, firm_id=firm_id_str
        )

    user.ms_access_token_ciphertext = encrypt_str(
        new_access_token, firm_id=firm_id_str
    )
    user.ms_token_expires_at = expires_at

    await append_audit(
        session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action="graph.token_refreshed",
        payload={
            "user_id": user_id_str,
            "expires_at": expires_at.isoformat(),
        },
    )
    return new_access_token


async def _audit_failure_and_commit(
    session: AsyncSession,
    firm_id: str,
    user_id: str,
    reason: str,
) -> None:
    """Append a graph.token_refresh_failed entry and commit.

    Committing here (not waiting for the caller) makes the failure
    audit durable even if the caller's session is torn down by the
    FastAPI exception handler before any commit of its own.
    """
    await append_audit(
        session,
        firm_id=firm_id,
        actor_type="user",
        actor_id=user_id,
        action="graph.token_refresh_failed",
        payload={"user_id": user_id, "reason": reason},
    )
    await session.commit()
