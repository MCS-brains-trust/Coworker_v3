"""Shared Microsoft Graph response handling.

Every Graph endpoint module (``mail``, ``calendar``, soon ``drive``
and ``profile``) maps the same handful of HTTP outcomes to the same
``ConnectorError`` subclasses and writes the same shape of failure
audit row. Centralising that logic here keeps the mapping identical
across endpoints and makes the audit chain consistent for anyone
querying ``WHERE action LIKE 'graph.%_failed'``.

Three callables make up the surface:

- ``raise_for_graph_status`` — inspect an ``httpx.Response``, return
  on 2xx, audit and raise the appropriate ``ConnectorError`` for
  every non-2xx status code.
- ``audit_failure`` — append a ``<action>_failed`` audit row with a
  standard payload and commit. Network-error paths use this directly
  because there's no response to inspect.
- ``parse_retry_after`` — parse Graph's integer-seconds Retry-After
  header, returning None for missing or non-numeric values.

The status→exception mapping is Graph-specific (Microsoft's status
codes and headers); Anthropic / XPM / FuseSign translate from their
own SDKs to the shared ``ConnectorError`` family in their own
connector modules.
"""
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.security.audit import append_audit


async def raise_for_graph_status(
    response: httpx.Response,
    *,
    session: AsyncSession,
    firm_id: str,
    user_id: str,
    action: str,
    allow_not_found: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    """Audit + raise the right ConnectorError for a non-2xx Graph response.

    Returns silently on 2xx. Every non-2xx code emits a
    ``<action>_failed`` audit row before raising.

    Args:
        response: the Graph response to inspect.
        session: caller's AsyncSession; the failure audit row commits
            on it before raising so the row survives any subsequent
            rollback when FastAPI propagates the exception.
        firm_id, user_id: identifiers stamped on the audit row.
        action: dotted action name, e.g. ``graph.mail.get_message``.
            ``_failed`` is appended for the audit action.
        allow_not_found: True for fetch-by-id endpoints where 404 is a
            real "the thing was deleted" condition (raises
            ``ConnectorNotFound``); False for list endpoints where
            404 shouldn't happen and would be a misconfigured caller
            (treated as auth-class for safety).
        extra: optional dict merged into the audit row's payload —
            typically the id of the resource being fetched.

    Raises:
        ConnectorNotFound: 404 and ``allow_not_found=True``.
        ConnectorAuthError: 401, 403, or any other unhandled 4xx
            (including 404 when ``allow_not_found=False``).
        ConnectorRateLimited: 429. ``retry_after`` parsed from the
            Retry-After header when numeric.
        ConnectorTransient: 5xx.
    """
    status = response.status_code
    if 200 <= status < 300:
        return

    if status == 404 and allow_not_found:
        await audit_failure(
            session,
            firm_id=firm_id,
            user_id=user_id,
            action=action,
            reason="microsoft_404",
            extra=extra,
        )
        raise ConnectorNotFound(f"Microsoft Graph returned 404 for {action}")
    if status == 401 or status == 403:
        await audit_failure(
            session,
            firm_id=firm_id,
            user_id=user_id,
            action=action,
            reason=f"microsoft_{status}",
            extra=extra,
        )
        raise ConnectorAuthError(
            f"Microsoft Graph rejected request: HTTP {status}"
        )
    if status == 429:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        await audit_failure(
            session,
            firm_id=firm_id,
            user_id=user_id,
            action=action,
            reason="microsoft_429",
            extra=extra,
        )
        raise ConnectorRateLimited(retry_after=retry_after)
    if 500 <= status < 600:
        await audit_failure(
            session,
            firm_id=firm_id,
            user_id=user_id,
            action=action,
            reason="microsoft_5xx",
            extra=extra,
        )
        raise ConnectorTransient(f"Microsoft Graph returned {status}")

    # Anything else 4xx (e.g. 400 bad query) — treat as auth-class for
    # now (caller can't recover automatically). A finer-grained
    # ConnectorPermanent may join the taxonomy when XPM / FuseSign
    # need to distinguish "your input is wrong" from "your token is
    # wrong" — for Microsoft those both arrive as 4xx with mostly
    # uninformative bodies.
    await audit_failure(
        session,
        firm_id=firm_id,
        user_id=user_id,
        action=action,
        reason=f"microsoft_{status}",
        extra=extra,
    )
    raise ConnectorAuthError(f"Microsoft Graph returned {status}")


async def audit_failure(
    session: AsyncSession,
    *,
    firm_id: str,
    user_id: str,
    action: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append ``<action>_failed`` with a standard payload and commit.

    The commit is inline so the audit row survives any subsequent
    rollback in the request scope (FastAPI exception propagation may
    discard the session before it commits). Same pattern as
    ``refresh_access_token``'s failure path in ``graph.auth``.
    """
    payload: dict[str, Any] = {"user_id": user_id, "reason": reason}
    if extra:
        payload.update(extra)
    await append_audit(
        session,
        firm_id=firm_id,
        actor_type="user",
        actor_id=user_id,
        action=f"{action}_failed",
        payload=payload,
    )
    await session.commit()


def parse_retry_after(header: str | None) -> float | None:
    """Parse Microsoft Graph's Retry-After header into seconds.

    Graph returns integer seconds in practice; HTTP-date form is rare
    and we don't parse it. Callers seeing ``retry_after=None`` apply
    their own default backoff.
    """
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None
