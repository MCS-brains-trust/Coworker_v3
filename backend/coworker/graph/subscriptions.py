"""Microsoft Graph change-notification subscriptions and the app-only context.

Two-part module:

1. ``graph_app_context`` — factory that acquires an app-only token via
   the client_credentials grant on the firm's Azure AD app and
   returns an ``AppGraphContext``. Used by background workflows that
   don't act on behalf of a signed-in user: webhook subscription
   renewal (Phase 11), the SharePoint indexer (Phase 4), the nightly
   reflection job, etc.

2. ``subscribe_change_notifications`` / ``renew_subscription`` —
   operate on Graph's ``/subscriptions`` collection to create and
   extend change-notification subscriptions. Microsoft caps message
   subscriptions at ~3 days, so Phase 11's hourly renewal job is the
   primary consumer of ``renew_subscription``.

**Not shadow-guarded.** Subscriptions are observation infrastructure,
not firm-data writes. A shadow-mode firm still needs subscriptions to
flow events into the queue so the system can prepare (would-be)
drafts; the drafts themselves remain shadow-blocked at the
``create_draft`` boundary.

**Token cache.** Per-process dict with a 5-minute refresh buffer,
guarded by an ``asyncio.Lock`` to prevent stampedes when a worker
wakes up with many concurrent renewals. Multi-worker deployments
see each worker cache independently; per-firm token endpoint usage
is bounded by approximately one fetch per (worker x hour). A Redis-
backed shared cache becomes worthwhile when we run >2 workers per
firm; for Phase 3 the in-process cache is acceptable.

**Audit shape.** Every subscribe / renew is audited with
``actor_type="system"`` and ``actor_id="system"`` — there is no
signed-in user. Token acquisition itself is NOT audited (loud raise
on failure, silent on success); the calling subscribe/renew row
captures the outcome of any work that depended on the token.
"""
import asyncio
import datetime as _dt
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorTransient,
)
from coworker.db.models.tenancy import Firm
from coworker.graph.errors import audit_failure, raise_for_graph_status
from coworker.graph.rate_limit import get_rate_limiter
from coworker.security.audit import append_audit
from coworker.security.encryption import decrypt_str

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_SUBSCRIPTIONS_ENDPOINT = f"{_GRAPH_ROOT}/subscriptions"
_LOGIN_HOST = "https://login.microsoftonline.com"
_TOKEN_REFRESH_BUFFER = _dt.timedelta(minutes=5)
_DEFAULT_EXPIRES_IN_SECONDS = 3600

# Synthetic actor for the audit log when there is no signed-in user.
# The audit_log.actor_type column carries "system"; this string fills
# the actor_id column and the payload's user_id field. A future
# refinement might use the firm's Azure app id, but "system" is
# unambiguous in the chain and avoids leaking app identifiers into
# the audit body.
SYSTEM_ACTOR = "system"

# Per-process app-token cache. firm_id (str) -> (access_token, expires_at).
# Guarded by ``_app_token_lock`` for concurrent fetches.
_app_token_cache: dict[str, tuple[str, _dt.datetime]] = {}
_app_token_lock = asyncio.Lock()


@dataclass(frozen=True, repr=False)
class AppGraphContext:
    """Per-firm Graph context for service-account / system workflows.

    Carries an app-only access token (acquired via client_credentials
    against the firm's Azure AD app), the firm row, and the session.
    Distinct from ``GraphContext`` because no signed-in user is
    involved — audit rows use ``actor_type="system"``, not a user_id.
    """

    firm: Firm
    access_token: str
    session: AsyncSession

    def __repr__(self) -> str:
        return (
            f"AppGraphContext(firm_id={self.firm.id}, "
            "access_token=<redacted>)"
        )


class Subscription(BaseModel):
    """A Microsoft Graph change-notification subscription.

    Stable contract for Phase 11's renewal job and any plugin that
    inspects active subscriptions. The fields are the subset of
    Graph's ``subscription`` resource we model; raw response fields
    we don't surface (``includeResourceData``, ``encryptionCertificate``,
    ``latestSupportedTlsVersion``) can be added when first needed.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    resource: str
    change_type: str
    notification_url: str
    expiration_date_time: _dt.datetime
    client_state: str | None = None
    application_id: str | None = None
    creator_id: str | None = None


async def graph_app_context(
    session: AsyncSession, firm: Firm
) -> AppGraphContext:
    """Acquire (or reuse a cached) app-only Graph token for ``firm``.

    Args:
        session: AsyncSession the caller is using. Stored on the
            returned context so subscribe/renew can write audit rows
            without an extra parameter.
        firm: target firm. Must have ``azure_tenant_id``,
            ``azure_client_id``, and ``azure_client_secret_ciphertext``
            populated (set during the onboarding wizard / first OAuth).

    Returns:
        ``AppGraphContext`` with a valid (cache-hit or freshly fetched)
        access token.

    Raises:
        ValueError: any of the three Azure credentials are missing on
            the firm row.
        ConnectorAuthError: Microsoft rejected the credentials (4xx
            from login endpoint — most often invalid_client / 400 or
            unauthorized_client / 401).
        ConnectorTransient: 5xx from login endpoint, network error,
            or timeout.
    """
    if not firm.azure_tenant_id:
        raise ValueError(
            f"firm {firm.id} has no azure_tenant_id; cannot acquire app token"
        )
    if not firm.azure_client_id:
        raise ValueError(
            f"firm {firm.id} has no azure_client_id; cannot acquire app token"
        )
    if firm.azure_client_secret_ciphertext is None:
        raise ValueError(
            f"firm {firm.id} has no azure_client_secret; cannot acquire app token"
        )

    token = await _resolve_app_token(firm)
    return AppGraphContext(firm=firm, access_token=token, session=session)


async def _resolve_app_token(firm: Firm) -> str:
    """Return a valid app-only token for ``firm``, fetching if needed.

    Cache check + fetch are serialised behind ``_app_token_lock`` to
    avoid stampedes: when a worker wakes up with N concurrent
    subscription renewals, only one fetch happens per firm. Per-firm
    contention is low enough that a single global lock is acceptable.
    """
    firm_id_str = str(firm.id)
    async with _app_token_lock:
        cached = _app_token_cache.get(firm_id_str)
        if cached is not None:
            token, expires_at = cached
            if expires_at > _dt.datetime.now(_dt.UTC) + _TOKEN_REFRESH_BUFFER:
                return token
        token, expires_at = await _fetch_app_token(firm)
        _app_token_cache[firm_id_str] = (token, expires_at)
        return token


async def _fetch_app_token(firm: Firm) -> tuple[str, _dt.datetime]:
    """Run the client_credentials grant against the firm's tenant.

    Returns ``(access_token, expires_at)``. Token endpoint failures
    are NOT audited at this layer — callers (subscribe/renew) audit
    their own outcome, and a token-fetch failure simply propagates
    as ``ConnectorAuthError`` / ``ConnectorTransient`` from the
    calling function. Loguru receives the raise via standard
    exception logging.
    """
    # mypy: the None-checks happen in graph_app_context; this private
    # helper is only reachable with all three present.
    assert firm.azure_tenant_id is not None
    assert firm.azure_client_id is not None
    assert firm.azure_client_secret_ciphertext is not None

    secret = decrypt_str(
        firm.azure_client_secret_ciphertext, firm_id=str(firm.id)
    )
    url = f"{_LOGIN_HOST}/{quote(firm.azure_tenant_id, safe='')}/oauth2/v2.0/token"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(
                url,
                data={
                    "client_id": firm.azure_client_id,
                    "client_secret": secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
    except httpx.RequestError as exc:
        raise ConnectorTransient(
            "network error fetching app token"
        ) from exc

    status = response.status_code
    if 200 <= status < 300:
        body = response.json()
        token: str = body["access_token"]
        expires_in = int(body.get("expires_in", _DEFAULT_EXPIRES_IN_SECONDS))
        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
            seconds=expires_in
        )
        return token, expires_at
    if 500 <= status < 600:
        raise ConnectorTransient(
            f"Microsoft login returned {status} fetching app token"
        )
    # 4xx — invalid_client, unauthorized_client, etc. The body has the
    # specific error code but it's not safe to include in the
    # exception message (could echo secrets in malformed requests).
    raise ConnectorAuthError(
        f"Microsoft login rejected app credentials: HTTP {status}"
    )


async def subscribe_change_notifications(
    ctx: AppGraphContext,
    *,
    resource: str,
    notification_url: str,
    expiration_date_time: _dt.datetime,
    client_state: str,
    change_type: str = "created,updated",
    lifecycle_notification_url: str | None = None,
) -> Subscription:
    """Create a Graph change-notification subscription.

    Args:
        ctx: app-only Graph context (subscriptions require app-only
            permissions in production; the API technically accepts
            delegated too, but long-lived webhooks shouldn't be tied
            to a user session).
        resource: Graph resource path to monitor, e.g.
            ``users/{userId}/mailFolders('Inbox')/messages``.
        notification_url: HTTPS endpoint Microsoft will POST changes
            to. Phase 11 wires this to ``coworker-webhook``.
        expiration_date_time: when the subscription expires. Must be
            tz-aware. Microsoft caps message subscriptions at ~3 days
            from now; longer values are rejected with 400.
        client_state: secret echoed back in every notification so the
            webhook receiver can validate the source. Not logged.
        change_type: comma-separated change types. Default
            ``"created,updated"``.
        lifecycle_notification_url: optional separate endpoint for
            ``subscriptionRemoved`` / ``reauthorizationRequired`` /
            ``missed`` events. In production we point this at the
            same webhook URL as ``notification_url`` so the handler
            discriminates internally. When None, Microsoft sends
            no lifecycle events.

    Returns:
        ``Subscription`` carrying ``id`` (used to renew/delete later)
        and the echoed expiration_date_time.

    Raises:
        ConnectorAuthError, ConnectorRateLimited, ConnectorTransient,
        ConnectorNotFound: standard Graph error mapping.
        ValueError: empty ``resource`` / ``notification_url`` /
            ``client_state``; tz-naive ``expiration_date_time``.
    """
    if not resource:
        raise ValueError("resource must be non-empty")
    if not notification_url:
        raise ValueError("notification_url must be non-empty")
    if not client_state:
        raise ValueError("client_state must be non-empty")
    if expiration_date_time.tzinfo is None:
        raise ValueError("expiration_date_time must be tz-aware")

    firm_id_str = str(ctx.firm.id)
    action = "graph.subscriptions.subscribe"
    # client_state is intentionally NOT included in extra — it's the
    # secret that validates notifications and must not enter the
    # audit chain.
    extra: dict[str, Any] = {
        "resource": resource,
        "notification_url": notification_url,
    }

    payload: dict[str, Any] = {
        "changeType": change_type,
        "notificationUrl": notification_url,
        "resource": resource,
        "expirationDateTime": _to_iso_z(expiration_date_time),
        "clientState": client_state,
    }
    if lifecycle_notification_url:
        payload["lifecycleNotificationUrl"] = lifecycle_notification_url
        extra["lifecycle_notification_url"] = lifecycle_notification_url

    response = await _post_graph_json(
        ctx,
        url=_SUBSCRIPTIONS_ENDPOINT,
        payload=payload,
        action=action,
        extra=extra,
        allow_not_found=True,
    )
    subscription = _parse_subscription(response.json())

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="system",
        actor_id=SYSTEM_ACTOR,
        action=action,
        payload={
            "user_id": SYSTEM_ACTOR,
            "subscription_id": subscription.id,
            "resource": resource,
            "notification_url": notification_url,
            "expiration_date_time": _to_iso_z(subscription.expiration_date_time),
        },
    )
    await ctx.session.commit()

    return subscription


async def renew_subscription(
    ctx: AppGraphContext,
    subscription_id: str,
    *,
    expiration_date_time: _dt.datetime,
) -> Subscription:
    """Extend an existing subscription's expiration.

    Args:
        ctx: app-only Graph context.
        subscription_id: the id returned by ``subscribe_change_notifications``.
        expiration_date_time: new expiration; tz-aware.

    Returns:
        Updated ``Subscription``.

    Raises:
        ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
        ConnectorNotFound: subscription was deleted (expired beyond
            grace, or revoked by an admin). Caller should re-create.
        ValueError: empty ``subscription_id`` or tz-naive expiration.
    """
    if not subscription_id:
        raise ValueError("subscription_id must be non-empty")
    if expiration_date_time.tzinfo is None:
        raise ValueError("expiration_date_time must be tz-aware")

    firm_id_str = str(ctx.firm.id)
    action = "graph.subscriptions.renew"
    extra: dict[str, Any] = {"subscription_id": subscription_id}

    url = f"{_SUBSCRIPTIONS_ENDPOINT}/{quote(subscription_id, safe='')}"
    payload = {"expirationDateTime": _to_iso_z(expiration_date_time)}

    response = await _patch_graph_json(
        ctx,
        url=url,
        payload=payload,
        action=action,
        extra=extra,
        allow_not_found=True,
    )
    subscription = _parse_subscription(response.json())

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="system",
        actor_id=SYSTEM_ACTOR,
        action=action,
        payload={
            "user_id": SYSTEM_ACTOR,
            "subscription_id": subscription.id,
            "expiration_date_time": _to_iso_z(subscription.expiration_date_time),
        },
    )
    await ctx.session.commit()

    return subscription


async def delete_subscription(
    ctx: AppGraphContext, subscription_id: str,
) -> bool:
    """Delete a Graph change-notification subscription.

    Used by the Phase 11-9 cleanup path when a user is deactivated:
    we tell Microsoft to stop sending notifications, then the
    caller removes the local row.

    Returns:
        True if Graph returned 204 (deleted). False if Graph
        returned 404 (already gone — treated as success because
        the desired end state is reached either way).

    Raises:
        ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
        ValueError: empty ``subscription_id``.
    """
    if not subscription_id:
        raise ValueError("subscription_id must be non-empty")

    firm_id_str = str(ctx.firm.id)
    action = "graph.subscriptions.delete"
    extra: dict[str, Any] = {"subscription_id": subscription_id}

    url = f"{_SUBSCRIPTIONS_ENDPOINT}/{quote(subscription_id, safe='')}"
    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(firm_id_str):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.delete(
                    url,
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=SYSTEM_ACTOR,
                action=action,
                reason="network_error",
                extra=extra,
                actor_type="system",
            )
            raise ConnectorTransient(
                "network error deleting subscription"
            ) from exc

    if response.status_code == 404:
        # Already gone; the local row is stale. Record the no-op
        # in audit so observability sees the deletion intent.
        await append_audit(
            ctx.session,
            firm_id=firm_id_str,
            actor_type="system",
            actor_id=SYSTEM_ACTOR,
            action=action,
            payload={
                "user_id": SYSTEM_ACTOR,
                "subscription_id": subscription_id,
                "outcome": "already_deleted",
            },
        )
        await ctx.session.commit()
        return False

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=SYSTEM_ACTOR,
        action=action,
        allow_not_found=False,
        extra=extra,
        actor_type="system",
    )

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="system",
        actor_id=SYSTEM_ACTOR,
        action=action,
        payload={
            "user_id": SYSTEM_ACTOR,
            "subscription_id": subscription_id,
            "outcome": "deleted",
        },
    )
    await ctx.session.commit()
    return True


async def _post_graph_json(
    ctx: AppGraphContext,
    *,
    url: str,
    payload: dict[str, Any],
    action: str,
    extra: dict[str, Any],
    allow_not_found: bool,
) -> httpx.Response:
    """POST JSON to Graph as the application; audit + map errors."""
    return await _request_graph_json(
        ctx,
        method="POST",
        url=url,
        payload=payload,
        action=action,
        extra=extra,
        allow_not_found=allow_not_found,
    )


async def _patch_graph_json(
    ctx: AppGraphContext,
    *,
    url: str,
    payload: dict[str, Any],
    action: str,
    extra: dict[str, Any],
    allow_not_found: bool,
) -> httpx.Response:
    """PATCH JSON to Graph as the application; audit + map errors."""
    return await _request_graph_json(
        ctx,
        method="PATCH",
        url=url,
        payload=payload,
        action=action,
        extra=extra,
        allow_not_found=allow_not_found,
    )


async def _request_graph_json(
    ctx: AppGraphContext,
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
    action: str,
    extra: dict[str, Any],
    allow_not_found: bool,
) -> httpx.Response:
    """Shared POST/PATCH plumbing for app-only Graph writes."""
    firm_id_str = str(ctx.firm.id)
    rate_limiter = get_rate_limiter()
    # No mailbox for system ops; key the per-mailbox semaphore on the
    # firm id so all of a firm's background calls share one slot
    # group.
    async with rate_limiter.slot(firm_id_str):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.request(
                    method,
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {ctx.access_token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=SYSTEM_ACTOR,
                action=action,
                reason="network_error",
                extra=extra,
                actor_type="system",
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=SYSTEM_ACTOR,
        action=action,
        allow_not_found=allow_not_found,
        extra=extra,
        actor_type="system",
    )
    return response


def _parse_subscription(raw: dict[str, Any]) -> Subscription:
    """Map a Graph subscription resource into our ``Subscription`` model."""
    return Subscription(
        id=raw["id"],
        resource=raw.get("resource", ""),
        change_type=raw.get("changeType", ""),
        notification_url=raw.get("notificationUrl", ""),
        expiration_date_time=_parse_iso_z(raw["expirationDateTime"]),
        client_state=raw.get("clientState"),
        application_id=raw.get("applicationId"),
        creator_id=raw.get("creatorId"),
    )


def _to_iso_z(dt: _dt.datetime) -> str:
    """Convert a tz-aware datetime to UTC ISO-8601 with trailing Z."""
    utc = dt.astimezone(_dt.UTC).replace(tzinfo=None)
    return utc.isoformat(timespec="seconds") + "Z"


def _parse_iso_z(value: str) -> _dt.datetime:
    """Parse Graph's ``Z``-suffixed ISO-8601 into a tz-aware datetime.

    Graph sometimes returns more than six fractional-second digits;
    trim before handing off to ``fromisoformat``.
    """
    cleaned = value.replace("Z", "+00:00")
    if "." in cleaned:
        base, rest = cleaned.split(".", 1)
        # rest is "fffffffff+00:00"; split off the offset.
        if "+" in rest:
            frac, offset = rest.split("+", 1)
            offset = "+" + offset
        elif "-" in rest:
            frac, offset = rest.split("-", 1)
            offset = "-" + offset
        else:
            frac, offset = rest, ""
        cleaned = f"{base}.{frac[:6]}{offset}"
    return _dt.datetime.fromisoformat(cleaned)
