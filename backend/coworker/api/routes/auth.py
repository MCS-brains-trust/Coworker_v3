"""Microsoft OAuth 2.0 authorization-code flow with PKCE.

Four routes (all under /api/v1/auth):

  GET /api/v1/auth/me
    Return the authenticated principal's identity (uses the session
    cookie). The frontend's CurrentUserProvider hits this on mount.

  GET /api/v1/auth/microsoft/start/{firm_slug}
    Begin a sign-in attempt. Looks up the firm by slug, generates a
    PKCE state token + code_verifier in Redis (10-min TTL), and
    redirects the browser to login.microsoftonline.com using the
    firm's tenant_id and client_id.

  GET /api/v1/auth/microsoft/callback?code=...&state=...
    Microsoft redirects here with an auth code. We atomically pop the
    state (replay-protected via GETDEL), look up the firm, decrypt
    the firm's Azure client secret, exchange the code for tokens,
    decode the id_token to read the user's identity claims, upsert
    the User row with both access_token and refresh_token encrypted
    under the firm's AAD, append an audit-log entry, mint a session
    JWT, set the cookie, and redirect to OAUTH_POST_LOGIN_REDIRECT.

  POST /api/v1/auth/logout
    Clear the session cookie. Microsoft refresh-token revocation is
    intentionally not implemented here: that requires a Graph API
    call and belongs with the Phase 3 Graph client wrapper.

Audit / loguru split
--------------------
Failures BEFORE consume_state succeeds (or where the firm row has been
deleted between /start and /callback) cannot be appended to the audit
chain because audit_log.firm_id is NOT NULL — events that never
reached a firm don't belong in the chain. Those failures are logged
to loguru with structured fields. Failures AFTER consume_state has
returned a firm_id are appended to the audit log under that firm.

Failure paths return the same generic HTTP response and detail string
regardless of the underlying reason — the *internal* reason
distinguishes them in logs/audit, the *external* response does not.
"""
import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.api.deps import current_user
from coworker.config import get_settings
from coworker.db.firms import lookup_firm_by_slug
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import firm_context, get_session
from coworker.security.audit import append_audit
from coworker.security.auth import (
    build_auth_url,
    decode_id_token_unverified,
    exchange_code,
)
from coworker.security.encryption import decrypt_str, encrypt_str
from coworker.security.oauth_state import (
    OAuthStateError,
    consume_state,
    create_state,
)
from coworker.security.session import (
    DEFAULT_TTL_SECONDS as SESSION_TTL_SECONDS,
)
from coworker.security.session import (
    issue_session_jwt,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_SESSION_COOKIE_NAME = "coworker_session"
_GENERIC_AUTH_FAILURE_DETAIL = "authentication failed"


def _client_log_fields(request: Request) -> dict[str, object]:
    """Best-effort fields for structured logging of un-auditable failures."""
    user_agent = request.headers.get("user-agent", "")
    return {
        "remote_ip": request.client.host if request.client else None,
        "user_agent": user_agent[:200],
    }


class CurrentUserResponse(BaseModel):
    """Outbound /api/v1/auth/me shape.

    Mirrors the User row but omits the per-firm Microsoft tokens:
    the principal's browser doesn't need them. Firm slug is
    denormalised so the frontend can build
    "/api/v1/auth/microsoft/start/{slug}" re-auth links without a
    follow-up call.
    """

    user_id: str
    firm_id: str
    firm_slug: str
    upn: str
    display_name: str
    role: str


@router.get("/me", response_model=CurrentUserResponse)
async def me(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CurrentUserResponse:
    """Return the authenticated principal's identity.

    The frontend's CurrentUserProvider hits this on mount to
    distinguish "signed in" from "401, redirect to login." Same
    generic 401 shape as every other auth-required endpoint when
    the cookie is missing, forged, or expired.
    """
    async with firm_context(user.firm_id):
        firm = (
            await session.execute(select(Firm).where(Firm.id == user.firm_id))
        ).scalar_one()
    return CurrentUserResponse(
        user_id=str(user.id),
        firm_id=str(user.firm_id),
        firm_slug=firm.slug,
        upn=user.upn,
        display_name=user.display_name,
        role=user.role,
    )


@router.get("/microsoft/start/{firm_slug}")
async def auth_start(
    firm_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    firm = await lookup_firm_by_slug(session, firm_slug)
    if firm is None:
        raise HTTPException(status_code=404, detail="firm not found")
    if not (
        firm.azure_tenant_id
        and firm.azure_client_id
        and firm.azure_client_secret_ciphertext
    ):
        raise HTTPException(
            status_code=409, detail="firm has no Azure credentials configured"
        )

    state_token, code_verifier = await create_state(firm.id)

    redirect_uri = str(request.url_for("auth_callback"))
    url = build_auth_url(
        firm_tenant_id=firm.azure_tenant_id,
        firm_client_id=firm.azure_client_id,
        redirect_uri=redirect_uri,
        state=state_token,
        code_verifier=code_verifier,
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/microsoft/callback", name="auth_callback")
async def auth_callback(
    code: str,
    state: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    # 1) State consumption. No firm context yet, so failures here go to
    #    loguru, not the audit chain.
    try:
        firm_id, code_verifier = await consume_state(state)
    except OAuthStateError:
        logger.warning(
            "auth.callback failed",
            reason="invalid_state",
            **_client_log_fields(request),
        )
        raise HTTPException(
            status_code=400, detail=_GENERIC_AUTH_FAILURE_DETAIL
        )

    # 2) Firm-row load. firm_context is set so RLS scopes the SELECT.
    async with firm_context(firm_id):
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one_or_none()
        if firm is None:
            # Firm deleted between /start and /callback. Same generic
            # response as invalid_state — don't leak whether the firm
            # ever existed.
            logger.warning(
                "auth.callback failed",
                reason="firm_not_found",
                **_client_log_fields(request),
            )
            raise HTTPException(
                status_code=400, detail=_GENERIC_AUTH_FAILURE_DETAIL
            )

        # Capture firm fields BEFORE any operation that might rollback
        # the transaction and expire the ORM instance.
        firm_id_str = str(firm.id)
        firm_slug = firm.slug
        firm_tenant_id = firm.azure_tenant_id
        firm_client_id = firm.azure_client_id
        client_secret = decrypt_str(
            firm.azure_client_secret_ciphertext, firm_id=firm_id_str
        )
        redirect_uri = str(request.url_for("auth_callback"))

        # 3) Code-for-tokens exchange. Failure auditable.
        try:
            token_response = await exchange_code(
                firm_tenant_id=firm_tenant_id,
                firm_client_id=firm_client_id,
                firm_client_secret=client_secret,
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )
        except Exception:
            await append_audit(
                session,
                firm_id=firm_id_str,
                actor_type="system",
                actor_id=None,
                action="auth.callback.failed",
                payload={"firm_slug": firm_slug, "reason": "token_exchange_failed"},
            )
            await session.commit()
            raise HTTPException(
                status_code=400, detail=_GENERIC_AUTH_FAILURE_DETAIL
            )

        id_claims = decode_id_token_unverified(token_response["id_token"])
        oid = id_claims.get("oid")
        upn = id_claims.get("preferred_username") or id_claims.get("upn")
        display_name = id_claims.get("name") or upn or oid
        if not oid or not upn:
            await append_audit(
                session,
                firm_id=firm_id_str,
                actor_type="system",
                actor_id=None,
                action="auth.callback.failed",
                payload={"firm_slug": firm_slug, "reason": "user_creation_failed"},
            )
            await session.commit()
            raise HTTPException(
                status_code=400, detail=_GENERIC_AUTH_FAILURE_DETAIL
            )

        # 4) Encrypt tokens with firm AAD; compute expiry.
        access_token_ct = encrypt_str(
            token_response["access_token"], firm_id=firm_id_str
        )
        refresh_token_ct = encrypt_str(
            token_response["refresh_token"], firm_id=firm_id_str
        )
        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
            seconds=int(token_response["expires_in"])
        )
        now = _dt.datetime.now(_dt.UTC)

        # 5) Upsert user row. Lookup by oid only — RLS scopes the SELECT
        # to this firm, and the schema has a global UNIQUE on
        # azure_object_id (so cross-firm collisions are rejected at
        # INSERT, see the IntegrityError branch).
        existing_user = (
            await session.execute(
                select(User).where(User.azure_object_id == oid)
            )
        ).scalar_one_or_none()

        try:
            if existing_user is None:
                user = User(
                    firm_id=firm.id,
                    azure_object_id=oid,
                    upn=upn,
                    display_name=display_name,
                    ms_access_token_ciphertext=access_token_ct,
                    ms_refresh_token_ciphertext=refresh_token_ct,
                    ms_token_expires_at=expires_at,
                    last_login_at=now,
                )
                session.add(user)
                await session.flush()
            else:
                existing_user.upn = upn
                existing_user.display_name = display_name
                existing_user.ms_access_token_ciphertext = access_token_ct
                existing_user.ms_refresh_token_ciphertext = refresh_token_ct
                existing_user.ms_token_expires_at = expires_at
                existing_user.last_login_at = now
                user = existing_user
                await session.flush()
        except IntegrityError:
            await session.rollback()
            await append_audit(
                session,
                firm_id=firm_id_str,
                actor_type="system",
                actor_id=None,
                action="auth.callback.failed",
                payload={
                    "firm_slug": firm_slug,
                    "reason": "azure_object_id_conflict",
                },
            )
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail="this account is associated with a different firm",
            )

        await append_audit(
            session,
            firm_id=firm_id_str,
            actor_type="user",
            actor_id=str(user.id),
            action="auth.callback.success",
            payload={"upn": upn, "user_id": str(user.id)},
        )
        await session.commit()

        jwt_token = issue_session_jwt(
            user_id=user.id, firm_id=firm.id, ttl_seconds=SESSION_TTL_SECONDS
        )

    settings = get_settings()
    response = RedirectResponse(
        url=settings.OAUTH_POST_LOGIN_REDIRECT, status_code=302
    )
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=jwt_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(settings.ENVIRONMENT != "dev"),
        path="/",
    )
    return response


@router.post("/logout", status_code=204)
async def auth_logout() -> Response:
    """Clear the session cookie locally.

    Microsoft refresh-token revocation against Graph is NOT implemented
    here: it requires an authenticated Graph call and belongs with the
    Phase 3 Microsoft Graph client wrapper. For now, logout invalidates
    the local session only; the Microsoft-side refresh token remains
    valid until its natural expiry or a tenant-side revocation.
    """
    settings = get_settings()
    response = Response(status_code=204)
    response.delete_cookie(
        key=_SESSION_COOKIE_NAME,
        path="/",
        secure=(settings.ENVIRONMENT != "dev"),
        httponly=True,
        samesite="lax",
    )
    return response
