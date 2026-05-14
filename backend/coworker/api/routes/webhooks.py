"""Microsoft Graph webhook receiver.

Two responsibilities:

1. **Subscription handshake** — when a new subscription is created
   via ``graph.subscriptions.subscribe_change_notifications``,
   Microsoft sends a POST with ``?validationToken=...`` and
   expects a 200 response with the plain-text token within 10
   seconds. We answer immediately and don't touch the queue.

2. **Notification dispatch** — Microsoft POSTs a JSON body with a
   ``value`` array of notifications. For each, we extract the
   triggering resource (typically a message id) and enqueue a
   ``PluginEvent`` to Redis. A separate worker pool fans out to
   the plugins listening to this trigger; the webhook stays
   intentionally thin so we always return 202 quickly (Microsoft
   times out at 30s and will throttle if we're slow).

Security
--------

Every notification is validated against the persisted
``graph_subscriptions`` row before enqueueing:

- Lookup the row by ``subscriptionId``. Unknown id → skip + log.
- Verify ``row.firm_id`` matches the firm resolved from the URL
  slug. Mismatch → skip + log (cross-firm replay defence).
- Decrypt ``row.client_state_ciphertext`` and compare against the
  notification's ``clientState`` (constant-time). Mismatch →
  skip + log.

Every per-notification rejection logs at WARNING and still
returns 202 — Microsoft retries on non-202, and a flood of
fabricated notifications shouldn't translate into a retry storm.

For unknown firm slugs the endpoint returns 202 (no enqueue,
no leak of which slugs exist).
"""
import datetime as _dt
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db import redis as redis_module
from coworker.db.firms import lookup_firm_by_slug
from coworker.db.models import GraphSubscription
from coworker.db.session import firm_context, get_session
from coworker.security.encryption import decrypt_str
from coworker.workers.plugin_queue import PluginEventQueue

router = APIRouter()


@router.post("/webhooks/graph/{firm_slug}")
async def graph_webhook(
    firm_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Receive a Graph change notification.

    Microsoft's contract:

    - For the handshake, the POST has ``?validationToken=...``
      query param and an empty body. The response body must be
      the plain-text token within 10s.
    - For a real notification, the body is JSON with a ``value``
      array of notification objects.

    We respond 202 to real notifications regardless of whether
    we enqueue anything — Microsoft retries on non-202, and a
    failure to enqueue (firm not found, malformed payload) is
    our problem to surface via logs and audit, not theirs to
    retry.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        logger.info(
            "graph webhook handshake firm_slug={} token={}",
            firm_slug,
            validation_token[:8] + "..." if len(validation_token) > 8
            else validation_token,
        )
        return PlainTextResponse(validation_token, status_code=200)

    try:
        body = await request.json()
    except ValueError:
        logger.warning(
            "graph webhook bad json firm_slug={}", firm_slug,
        )
        return Response(status_code=202)

    if not isinstance(body, dict):
        return Response(status_code=202)

    notifications = body.get("value") or []
    if not isinstance(notifications, list) or not notifications:
        return Response(status_code=202)

    firm = await lookup_firm_by_slug(session, firm_slug)
    if firm is None:
        # Don't leak whether a slug exists — log it but still 202.
        logger.warning(
            "graph webhook unknown firm_slug={}", firm_slug,
        )
        return Response(status_code=202)
    # Close the NO-FORCE-bracket transaction so the next reads
    # below pick up the firm_context GUC via after_begin.
    await session.commit()

    # Call through the module so test monkeypatching of get_redis
    # takes effect. ``from coworker.db.redis import get_redis``
    # would bind the original at import time and bypass the patch.
    queue = PluginEventQueue(redis_module.get_redis())

    enqueued = 0
    rejected = 0
    async with firm_context(firm.id):
        for notif in notifications:
            if not isinstance(notif, dict):
                continue
            sub_id = notif.get("subscriptionId")
            client_state = notif.get("clientState")
            if not isinstance(sub_id, str) or not isinstance(client_state, str):
                logger.warning(
                    "graph webhook missing sub/clientState firm_slug={}",
                    firm_slug,
                )
                rejected += 1
                continue
            if not await _validate_subscription(
                session,
                firm_id=firm.id,
                subscription_id=sub_id,
                client_state=client_state,
                firm_slug=firm_slug,
            ):
                rejected += 1
                continue
            event_data = _build_event_data(notif)
            if event_data is None:
                continue
            await queue.enqueue(
                trigger="email_received",
                firm_slug=firm_slug,
                firm_id=firm.id,
                event_data=event_data,
            )
            enqueued += 1

    if enqueued or rejected:
        logger.info(
            "graph webhook enqueued={} rejected={} firm_slug={}",
            enqueued, rejected, firm_slug,
        )

    return Response(status_code=202)


async def _validate_subscription(
    session: AsyncSession,
    *,
    firm_id: Any,
    subscription_id: str,
    client_state: str,
    firm_slug: str,
) -> bool:
    """Return True iff this notification matches a stored subscription.

    Three checks (any failure -> False, log, drop):

    1. A row exists for the given ``subscription_id``.
    2. ``row.firm_id`` matches the firm resolved from the URL slug
       — guards against an attacker who learned a real
       subscription_id but tries to direct notifications at a
       different firm's queue.
    3. The decrypted ``client_state_ciphertext`` matches the
       notification's ``clientState``, compared in constant time.
    """
    row = (
        await session.execute(
            select(GraphSubscription)
            .where(GraphSubscription.subscription_id == subscription_id)
        )
    ).scalar_one_or_none()
    if row is None:
        logger.warning(
            "graph webhook unknown subscription sub_id={} firm_slug={}",
            subscription_id, firm_slug,
        )
        return False
    if row.firm_id != firm_id:
        logger.warning(
            "graph webhook cross-firm subscription sub_id={} firm_slug={}",
            subscription_id, firm_slug,
        )
        return False
    try:
        expected = decrypt_str(
            row.client_state_ciphertext, firm_id=str(firm_id),
        )
    except Exception:
        logger.exception(
            "graph webhook decrypt client_state failed sub_id={}",
            subscription_id,
        )
        return False
    if not hmac.compare_digest(expected, client_state):
        logger.warning(
            "graph webhook bad clientState sub_id={} firm_slug={}",
            subscription_id, firm_slug,
        )
        return False
    return True


def _build_event_data(notification: dict[str, Any]) -> dict[str, Any] | None:
    """Build the PluginEvent event_data payload from one notification.

    Microsoft's notification shape:

        {
          "subscriptionId": "...",
          "clientState": "...",
          "changeType": "created" | "updated" | ...,
          "resource": "users/{userId}/messages/{messageId}",
          "resourceData": {
            "@odata.type": "#Microsoft.Graph.Message",
            "@odata.id": "...",
            "@odata.etag": "...",
            "id": "<message_id>"
          },
          ...
        }

    Returns None when the notification doesn't carry enough to
    construct a meaningful event (missing message id, unsupported
    resource type) — the receiver still returns 202 and logs.
    """
    resource_data = notification.get("resourceData")
    if not isinstance(resource_data, dict):
        return None
    message_id = resource_data.get("id")
    if not message_id:
        return None
    change_type = notification.get("changeType") or "created"
    return {
        "message_id": message_id,
        "change_type": change_type,
        "subscription_id": notification.get("subscriptionId"),
        "resource": notification.get("resource"),
        "received_at_webhook": _dt.datetime.now(_dt.UTC).isoformat(),
    }
