"""Xero Practice Manager (XPM) connector.

Single per-firm class. The only code path in this codebase that talks
to ``identity.xero.com`` (token endpoint) or ``api.xero.com`` (XPM
REST API).

OAuth shape (Xero's standard for confidential clients):

- Token endpoint: ``https://identity.xero.com/connect/token``.
- ``refresh_token`` grant authenticated with HTTP Basic
  (client_id : client_secret).
- Refresh tokens valid 60 days, **single-use** — every refresh returns
  a new refresh_token that replaces the previous one. The connector
  rotates the persisted ciphertext on every successful refresh; if
  the rotation isn't committed to the DB, the next refresh will fail
  with invalid_grant.
- Access tokens valid ~30 minutes. We refresh proactively at the
  5-minute mark so a slow downstream call doesn't time out mid-flight.

Audit shape:

- Success: ``xpm.token_refreshed`` with the new expiry.
- Failure: ``xpm.token_refresh_failed`` with the reason.

For Phase 3 the actor on these rows defaults to ``"system"`` because
the orchestrator and background workflows are the typical callers.
A user-initiated path (e.g. a future UI button "refresh XPM
connection") can construct an ``XPMClient`` with ``actor_type="user"``
and ``actor_id=str(user.id)`` and the audit row will reflect that.

This commit (Phase 3E-2) lands the OAuth scaffolding only —
``_ensure_access_token`` / ``_refresh_access_token`` plus the class
shape and credential checks. The read and write methods (
``list_clients``, ``get_client``, ``create_client_note``, etc.) land
in subsequent Phase 3E sub-commits.
"""
import datetime as _dt
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit
from coworker.security.encryption import decrypt_str, encrypt_str

_TOKEN_ENDPOINT = "https://identity.xero.com/connect/token"
# XPM's base URL. The "3.1" segment matches the current Practice
# Manager API version; if Xero versions the path we'll update one
# constant. The trailing slash is omitted so callers compose
# ``f"{_API_BASE}/{resource}"`` cleanly.
_API_BASE = "https://api.xero.com/practicemanager/3.1"

_TOKEN_REFRESH_BUFFER = _dt.timedelta(minutes=5)

# Defensive cap on pagination. XPM tenants with thousands of clients
# spread across hundreds of pages are realistic; 10,000 pages would
# indicate a server-side loop in the Link headers (or a bug here that
# fails to advance). Surfacing as a transient lets the caller retry
# with a tighter filter rather than spin forever.
_MAX_PAGINATION_PAGES = 10_000
# Xero documents 30-min access tokens, but the actual expires_in
# field on the response is authoritative. This default only applies
# if the response omits expires_in (it doesn't, in practice — kept
# for defensive parsing).
_DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 1800

SYSTEM_ACTOR = "system"


# NOTE: The exact XPM endpoint paths and response field names below
# are based on the documented Xero Practice Manager API surface as of
# 2026. They are URL-encoded into constants so that any drift surfaced
# during Phase 16A shadow testing can be fixed in one place. The
# connector's shape — auth, audit, error mapping, pagination — is the
# load-bearing contract; the specific paths can be adjusted without
# touching callers.
_CLIENTS_LIST_PATH = "clients.api/list"
_CLIENT_GET_PATH = "client.api/get"  # /{client_id}
_JOBS_LIST_PATH = "jobs.api/list"
_INVOICES_LIST_PATH = "invoices.api/list"
_INVOICE_GET_PATH = "invoice.api/get"  # /{invoice_id}
_RELATIONSHIPS_LIST_PATH = "relationships.api/list"


class XPMClientRecord(BaseModel):
    """An XPM Client (the firm's customer/contact record).

    Narrow projection of XPM's wide schema. Plugin code consumes this
    shape, not the raw Xero JSON, so the plugin layer stays insulated
    from upstream schema drift.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    email: str | None = None
    phone: str | None = None
    is_active: bool = True
    # Entity type (Individual / Company / Trust / Partnership / SMSF
    # / Sole Trader). Free-text from Xero — we don't constrain to a
    # Literal because XPM admins can configure their own types.
    entity_type: str | None = None
    created_at: _dt.datetime
    modified_at: _dt.datetime


class XPMJob(BaseModel):
    """An XPM Job (the firm's unit of work for a client — tax return,
    BAS lodgement, audit engagement, etc.).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    client_id: str
    # XPM admins configure their own job states; common values include
    # "In Progress", "Complete", "On Hold", "Cancelled". Free-text.
    state: str
    created_at: _dt.datetime
    due_at: _dt.datetime | None = None
    completed_at: _dt.datetime | None = None


class XPMInvoice(BaseModel):
    """An XPM Invoice. Amounts use ``Decimal`` for cent-precision."""

    model_config = ConfigDict(frozen=True)

    id: str
    number: str
    client_id: str
    total_amount: Decimal
    total_tax: Decimal
    currency: str  # ISO 4217 — typically "AUD" for MC&S
    status: str  # "Draft", "Approved", "Sent", "Paid", etc.
    issued_at: _dt.datetime
    due_at: _dt.datetime | None = None


class XPMRelationship(BaseModel):
    """A relationship edge between two XPM Clients.

    Drives the Phase 4 knowledge graph. ``relationship_type`` is
    free-text from XPM and includes values like ``"Director"``,
    ``"Trustee"``, ``"Beneficiary"``, ``"Appointor"``,
    ``"Shareholder"``, ``"Spouse"``. The KG populator maps these to
    its own canonical types.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    from_client_id: str
    to_client_id: str
    relationship_type: str
    is_active: bool = True


class XPMClient:
    """Per-firm XPM REST client.

    Construct once per (firm, session) pair. The class holds a
    reference to the Firm row so token refreshes mutate it in place;
    the caller's session is the one those mutations land on. Callers
    are responsible for committing the session — the refresh helper
    flushes but does not commit, so audit rows + token rotation land
    in the same transaction as whatever XPM call triggered the
    refresh.

    Args:
        firm: target firm. Must have ``xpm_client_id``,
            ``xpm_client_secret_ciphertext`` and
            ``xpm_refresh_token_ciphertext`` populated (set during
            the Phase 13 onboarding wizard).
        session: AsyncSession; used to persist refreshed tokens and
            write audit rows.
        actor_id: who's making this call. Defaults to ``"system"``
            because the orchestrator and background jobs are the
            typical callers. User-initiated paths pass
            ``str(user.id)`` and ``actor_type="user"``.
        actor_type: ``"system"`` (default) or ``"user"``.
    """

    def __init__(
        self,
        firm: Firm,
        *,
        session: AsyncSession,
        actor_id: str = SYSTEM_ACTOR,
        actor_type: Literal["user", "system"] = "system",
    ) -> None:
        self._firm = firm
        self._session = session
        self._actor_id = actor_id
        self._actor_type = actor_type

    @property
    def firm(self) -> Firm:
        return self._firm

    async def list_clients(
        self,
        *,
        updated_since: _dt.datetime | None = None,
    ) -> list[XPMClientRecord]:
        """List XPM clients (the firm's customer records).

        Args:
            updated_since: optional tz-aware datetime for incremental
                sync. When provided, only clients modified at-or-after
                the timestamp are returned. The Phase 4 KG populator
                uses this for nightly delta loads.

        Returns:
            All clients across however many pages the response is
            spread over. ``Link: rel="next"`` is followed transparently.

        Raises:
            ConnectorAuthError: 401 / 403 / other unhandled 4xx.
            ConnectorRateLimited: 429.
            ConnectorTransient: 5xx / network error.
            ValueError: ``updated_since`` is tz-naive.
        """
        if updated_since is not None and updated_since.tzinfo is None:
            raise ValueError("updated_since must be tz-aware")

        action = "xpm.clients.list"
        params: dict[str, Any] = {}
        if updated_since is not None:
            params["modifiedsince"] = (
                updated_since.astimezone(_dt.UTC)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds")
            )
        url = f"{_API_BASE}/{_CLIENTS_LIST_PATH}"
        extra: dict[str, Any] = {}
        if updated_since is not None:
            extra["modifiedsince"] = params["modifiedsince"]

        clients, pages = await self._list_all(
            url=url,
            params=params,
            action=action,
            extra=extra,
            envelope_key="Clients",
            parser=_parse_client_record,
        )

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "count": len(clients),
                "pages": pages,
                **extra,
            },
        )
        await self._session.commit()
        return clients

    async def get_client(self, client_id: str) -> XPMClientRecord:
        """Fetch one XPM client by id.

        Raises:
            ConnectorNotFound: 404 (client deleted or never existed).
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: ``client_id`` is empty.
        """
        if not client_id:
            raise ValueError("client_id must be non-empty")

        action = "xpm.clients.get"
        url = f"{_API_BASE}/{_CLIENT_GET_PATH}/{quote(client_id, safe='')}"
        extra: dict[str, Any] = {"client_id": client_id}

        response = await self._authenticated_get(
            url=url, params={}, action=action, extra=extra,
            allow_not_found=True,
        )
        body = response.json()
        record = _parse_client_record(_extract_single(body, key="Client"))

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "client_id": client_id,
            },
        )
        await self._session.commit()
        return record

    async def list_jobs(
        self, *, client_id: str | None = None
    ) -> list[XPMJob]:
        """List XPM jobs, optionally scoped to one client.

        Returns one page; pagination lands in 3E-5.

        Raises:
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: ``client_id`` is an empty string (distinguish
                from ``None``).
        """
        if client_id is not None and not client_id:
            raise ValueError("client_id must be non-empty when provided")

        action = "xpm.jobs.list"
        url = f"{_API_BASE}/{_JOBS_LIST_PATH}"
        params: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if client_id is not None:
            params["clientid"] = client_id
            extra["client_id"] = client_id

        jobs, pages = await self._list_all(
            url=url, params=params, action=action, extra=extra,
            envelope_key="Jobs", parser=_parse_job,
        )

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "count": len(jobs),
                "pages": pages,
                **extra,
            },
        )
        await self._session.commit()
        return jobs

    async def list_invoices(
        self, *, client_id: str | None = None
    ) -> list[XPMInvoice]:
        """List XPM invoices, optionally scoped to one client."""
        if client_id is not None and not client_id:
            raise ValueError("client_id must be non-empty when provided")

        action = "xpm.invoices.list"
        url = f"{_API_BASE}/{_INVOICES_LIST_PATH}"
        params: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if client_id is not None:
            params["clientid"] = client_id
            extra["client_id"] = client_id

        invoices, pages = await self._list_all(
            url=url, params=params, action=action, extra=extra,
            envelope_key="Invoices", parser=_parse_invoice,
        )

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "count": len(invoices),
                "pages": pages,
                **extra,
            },
        )
        await self._session.commit()
        return invoices

    async def get_invoice(self, invoice_id: str) -> XPMInvoice:
        """Fetch one XPM invoice by id.

        Raises:
            ConnectorNotFound: 404 (invoice deleted or never existed).
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: ``invoice_id`` is empty.
        """
        if not invoice_id:
            raise ValueError("invoice_id must be non-empty")

        action = "xpm.invoices.get"
        url = f"{_API_BASE}/{_INVOICE_GET_PATH}/{quote(invoice_id, safe='')}"
        extra: dict[str, Any] = {"invoice_id": invoice_id}

        response = await self._authenticated_get(
            url=url, params={}, action=action, extra=extra,
            allow_not_found=True,
        )
        invoice = _parse_invoice(
            _extract_single(response.json(), key="Invoice")
        )

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "invoice_id": invoice_id,
            },
        )
        await self._session.commit()
        return invoice

    async def list_relationships(
        self, client_id: str
    ) -> list[XPMRelationship]:
        """List relationship edges anchored on ``client_id``.

        Phase 4's KG populator uses this for each client to build the
        directed edges (director_of, trustee_of, beneficiary_of, etc.).
        """
        if not client_id:
            raise ValueError("client_id must be non-empty")

        action = "xpm.relationships.list"
        url = f"{_API_BASE}/{_RELATIONSHIPS_LIST_PATH}"
        params: dict[str, Any] = {"clientid": client_id}
        extra: dict[str, Any] = {"client_id": client_id}

        rels, pages = await self._list_all(
            url=url, params=params, action=action, extra=extra,
            envelope_key="Relationships", parser=_parse_relationship,
        )

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "client_id": client_id,
                "count": len(rels),
                "pages": pages,
            },
        )
        await self._session.commit()
        return rels

    async def _list_all(
        self,
        *,
        url: str,
        params: dict[str, Any],
        action: str,
        extra: dict[str, Any],
        envelope_key: str,
        parser: Any,
    ) -> tuple[list[Any], int]:
        """Follow ``Link: rel="next"`` until exhausted; return all records + page count.

        Each page goes through ``_authenticated_get`` separately, so a
        failure on page 5 of 7 produces a single ``<action>_failed``
        audit row for that page (not 5 success rows + 1 failure). The
        caller's single success audit row at the end carries the total
        count and ``pages`` so the audit log captures the size of the
        list as one logical operation.

        ``parser`` is a sync callable ``dict -> T`` applied to each
        envelope entry.
        """
        records: list[Any] = []
        pages = 0
        next_url: str | None = url
        # httpx treats ``params={}`` as "replace query string with
        # empty" — passing it on follow-up pages would strip the
        # ``?page=N`` Xero embedded in its next-link URL and we'd
        # loop on page 1 forever. ``None`` means "don't touch the
        # URL"; that's what we want for follow-up requests.
        next_params: dict[str, Any] | None = params
        while next_url is not None:
            if pages >= _MAX_PAGINATION_PAGES:
                raise ConnectorTransient(
                    f"XPM pagination exceeded {_MAX_PAGINATION_PAGES} pages "
                    f"for {action}; suspected upstream loop"
                )
            response = await self._authenticated_get(
                url=next_url,
                params=next_params,
                action=action,
                extra=extra,
            )
            raw_items = _extract_collection(response.json(), key=envelope_key)
            records.extend(parser(item) for item in raw_items)
            pages += 1
            next_url = _next_link_url(response.headers.get("Link"))
            next_params = None
        return records, pages

    async def _authenticated_get(
        self,
        *,
        url: str,
        params: dict[str, Any] | None,
        action: str,
        extra: dict[str, Any],
        allow_not_found: bool = False,
    ) -> httpx.Response:
        """GET with bearer token (refreshing if needed); audit + raise on error.

        ``params=None`` means "don't touch the URL's query string";
        an empty dict would strip an existing query string. Pass
        ``None`` when ``url`` already carries the query (e.g. a
        Link-header next-page URL), pass a dict to merge new params.

        Returns the response on 2xx. Callers parse ``response.json()``
        themselves so each method controls its own schema mapping.
        """
        access_token = await self._ensure_access_token()
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    url,
                    params=params,
                    headers=self._auth_headers(access_token),
                )
        except httpx.RequestError as exc:
            await self._audit_failure(
                action=action, reason="network_error", extra=extra
            )
            raise ConnectorTransient(
                "network error talking to XPM"
            ) from exc

        await self._raise_for_xero_status(
            response,
            action=action,
            allow_not_found=allow_not_found,
            extra=extra,
        )
        return response

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        """Standard auth + tenant headers for every XPM API call.

        Xero APIs require the ``Xero-Tenant-Id`` header to disambiguate
        which connected organisation the bearer token is acting on.
        The tenant id lives on the firm row as ``xpm_account_id``.
        Missing tenant id is a misconfigured firm; raise so the bug
        surfaces at the first call rather than as opaque 4xx from Xero.
        """
        tenant_id = self._firm.xpm_account_id
        if not tenant_id:
            raise ConnectorAuthError(
                f"firm {self._firm.id} has no xpm_account_id; cannot send Xero-Tenant-Id"
            )
        return {
            "Authorization": f"Bearer {access_token}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        }

    async def _ensure_access_token(self) -> str:
        """Return a non-expired XPM access token, refreshing if needed.

        Refresh trigger: ``xpm_token_expires_at`` is None, in the
        past, or within ``_TOKEN_REFRESH_BUFFER`` of now. Otherwise
        the cached ciphertext is decrypted and returned.
        """
        firm = self._firm
        now = _dt.datetime.now(_dt.UTC)
        expires_at = firm.xpm_token_expires_at

        if (
            expires_at is None
            or expires_at.tzinfo is None
            or expires_at <= now + _TOKEN_REFRESH_BUFFER
            or firm.xpm_access_token_ciphertext is None
        ):
            return await self._refresh_access_token()

        return decrypt_str(
            firm.xpm_access_token_ciphertext, firm_id=str(firm.id)
        )

    async def _refresh_access_token(self) -> str:
        """Run the refresh_token grant; rotate persisted tokens.

        Persists the new access and refresh tokens (Xero rotates the
        refresh token on every grant) plus the new expiry. Audits
        ``xpm.token_refreshed`` on success, ``xpm.token_refresh_failed``
        on every failure mode. Returns the new plaintext access token.

        Raises:
            ConnectorAuthError: missing credentials on the firm row,
                or Xero rejected the grant (4xx — typically
                ``invalid_grant`` when the refresh token is past 60
                days or has been revoked).
            ConnectorTransient: 5xx from Xero or network error.
        """
        firm = self._firm
        firm_id_str = str(firm.id)

        # Credential precondition checks. Surface as audited
        # ConnectorAuthError so the caller sees the same exception
        # family regardless of whether Xero rejected us or we never
        # had credentials to send.
        if firm.xpm_refresh_token_ciphertext is None:
            await self._audit_refresh_failure("missing_refresh_token")
            raise ConnectorAuthError(
                f"firm {firm.id} has no xpm_refresh_token; XPM not connected"
            )
        if (
            firm.xpm_client_id is None
            or firm.xpm_client_secret_ciphertext is None
        ):
            await self._audit_refresh_failure("missing_client_credentials")
            raise ConnectorAuthError(
                f"firm {firm.id} has no XPM client credentials"
            )

        # Decrypt outside the try/except so a corrupt-ciphertext error
        # is its own thing, not lumped with HTTP errors.
        try:
            refresh_token = decrypt_str(
                firm.xpm_refresh_token_ciphertext, firm_id=firm_id_str
            )
            client_secret = decrypt_str(
                firm.xpm_client_secret_ciphertext, firm_id=firm_id_str
            )
        except Exception:
            await self._audit_refresh_failure("corrupt_ciphertext")
            raise ConnectorAuthError(
                f"firm {firm.id} XPM ciphertext could not be decrypted"
            ) from None

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    _TOKEN_ENDPOINT,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    auth=(firm.xpm_client_id, client_secret),
                )
        except httpx.RequestError as exc:
            await self._audit_refresh_failure("network_error")
            raise ConnectorTransient(
                "network error talking to Xero identity"
            ) from exc

        status = response.status_code
        if 200 <= status < 300:
            body = response.json()
            new_access: str = body["access_token"]
            # Xero may or may not return a new refresh_token on every
            # grant. In their current implementation it always
            # rotates, but we defensively fall back to the existing
            # refresh token if the field is absent — that keeps the
            # firm row consistent if Xero ever changes behaviour.
            new_refresh: str = body.get("refresh_token", refresh_token)
            expires_in = int(
                body.get("expires_in", _DEFAULT_ACCESS_TOKEN_TTL_SECONDS)
            )

            firm.xpm_access_token_ciphertext = encrypt_str(
                new_access, firm_id=firm_id_str
            )
            firm.xpm_refresh_token_ciphertext = encrypt_str(
                new_refresh, firm_id=firm_id_str
            )
            firm.xpm_token_expires_at = (
                _dt.datetime.now(_dt.UTC)
                + _dt.timedelta(seconds=expires_in)
            )
            await self._session.flush()
            await append_audit(
                self._session,
                firm_id=firm_id_str,
                actor_type=self._actor_type,
                actor_id=self._actor_id,
                action="xpm.token_refreshed",
                payload={
                    "user_id": self._actor_id,
                    "expires_in": expires_in,
                },
            )
            await self._session.commit()
            return new_access

        if 500 <= status < 600:
            await self._audit_refresh_failure("xero_5xx")
            raise ConnectorTransient(
                f"Xero identity returned {status} on refresh"
            )
        # 4xx — invalid_grant (refresh token expired / revoked /
        # already used), invalid_client (credentials wrong), etc.
        await self._audit_refresh_failure(f"xero_{status}")
        raise ConnectorAuthError(
            f"Xero identity rejected refresh: HTTP {status}"
        )

    async def _audit_refresh_failure(self, reason: str) -> None:
        """Token-refresh failure audit. Action ``xpm.token_refresh_failed``."""
        await self._audit_failure(action="xpm.token_refresh", reason=reason)

    async def _audit_failure(
        self,
        *,
        action: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append ``<action>_failed`` for any XPM operation and commit.

        Committed inline so the audit row survives caller rollback —
        same pattern as ``graph.auth._audit_failure_and_commit``.
        Public XPM methods set their own action prefix (e.g.
        ``xpm.clients.list``) so the failed action becomes
        ``xpm.clients.list_failed``.
        """
        firm_id_str = str(self._firm.id)
        payload: dict[str, Any] = {
            "user_id": self._actor_id,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=f"{action}_failed",
            payload=payload,
        )
        await self._session.commit()

    async def _raise_for_xero_status(
        self,
        response: httpx.Response,
        *,
        action: str,
        allow_not_found: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Audit + raise the right ConnectorError for a non-2xx Xero response.

        Same shape as ``graph.errors.raise_for_graph_status`` but with
        ``xero_*`` reason prefixes. Returns silently on 2xx.

        Raises:
            ConnectorNotFound: 404 and ``allow_not_found=True``.
            ConnectorAuthError: 401 / 403 / other unhandled 4xx.
            ConnectorRateLimited: 429. ``retry_after`` from
                Retry-After header when numeric.
            ConnectorTransient: 5xx.
        """
        status = response.status_code
        if 200 <= status < 300:
            return

        if status == 404 and allow_not_found:
            await self._audit_failure(
                action=action, reason="xero_404", extra=extra
            )
            raise ConnectorNotFound(f"XPM returned 404 for {action}")
        if status == 401 or status == 403:
            await self._audit_failure(
                action=action, reason=f"xero_{status}", extra=extra
            )
            raise ConnectorAuthError(
                f"XPM rejected request: HTTP {status}"
            )
        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            await self._audit_failure(
                action=action, reason="xero_429", extra=extra
            )
            raise ConnectorRateLimited(retry_after=retry_after)
        if 500 <= status < 600:
            await self._audit_failure(
                action=action, reason="xero_5xx", extra=extra
            )
            raise ConnectorTransient(f"XPM returned {status}")

        # Other 4xx — treat as auth-class. Refine to ConnectorPermanent
        # when we encounter a real case worth distinguishing.
        await self._audit_failure(
            action=action, reason=f"xero_{status}", extra=extra
        )
        raise ConnectorAuthError(f"XPM returned {status}")


def _parse_retry_after(header: str | None) -> float | None:
    """Parse a numeric Retry-After header into seconds; None otherwise."""
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None


def _next_link_url(header: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from an RFC 5988 ``Link`` header.

    Format example::

        Link: <https://api.xero.com/.../list?page=2>; rel="next",
              <https://api.xero.com/.../list?page=1>; rel="prev"

    Returns the URL when a ``next`` relation is present, otherwise
    None. Tolerates ``rel=next`` (unquoted) and minor whitespace
    variation.
    """
    if header is None:
        return None
    for entry in header.split(","):
        entry = entry.strip()
        if 'rel="next"' not in entry and "rel=next" not in entry:
            continue
        start = entry.find("<")
        end = entry.find(">", start + 1) if start != -1 else -1
        if start == -1 or end == -1:
            continue
        return entry[start + 1 : end]
    return None


def _parse_xpm_datetime(value: str) -> _dt.datetime:
    """Parse Xero's ISO-8601 timestamp into a tz-aware ``datetime``.

    Xero sometimes returns naive ISO timestamps (no Z suffix); treat
    those as UTC. Anything with an explicit offset is preserved.
    """
    parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _extract_collection(body: Any, *, key: str) -> list[dict[str, Any]]:
    """Pull a list of records out of XPM's response envelope.

    Xero APIs use varied shapes. The Practice Manager API frequently
    returns ``{"Clients": [...]}`` or ``{"Response": {"Clients": [...]}}``.
    We accept either, plus a raw list, so the parser is forgiving of
    minor envelope changes.
    """
    if isinstance(body, list):
        items_list: list[dict[str, Any]] = body
        return items_list
    if isinstance(body, dict):
        if key in body and isinstance(body[key], list):
            top_items: list[dict[str, Any]] = body[key]
            return top_items
        # Nested under "Response"
        response = body.get("Response")
        if isinstance(response, dict) and isinstance(response.get(key), list):
            nested_items: list[dict[str, Any]] = response[key]
            return nested_items
    return []


def _extract_single(body: Any, *, key: str) -> dict[str, Any]:
    """Pull a single record out of XPM's response envelope.

    ``{key: {...}}`` or ``{"Response": {key: {...}}}`` or a raw object.
    Raises ``ValueError`` if the body doesn't match any expected shape.
    """
    if not isinstance(body, dict):
        raise ValueError(f"XPM returned non-object body where {key} expected")
    if key in body and isinstance(body[key], dict):
        record = body[key]
        assert isinstance(record, dict)
        return record
    response = body.get("Response")
    if isinstance(response, dict) and isinstance(response.get(key), dict):
        record = response[key]
        assert isinstance(record, dict)
        return record
    # Fall back to treating the whole body as the record (Xero
    # sometimes returns the bare object on /get endpoints).
    return body


def _parse_client_record(raw: dict[str, Any]) -> XPMClientRecord:
    """Map one XPM Client dict into an ``XPMClientRecord``.

    Field names follow Xero's PascalCase convention. Optional fields
    fall back to None / sensible defaults.
    """
    return XPMClientRecord(
        id=str(raw["ID"]),
        name=raw.get("Name") or "",
        email=raw.get("Email") or None,
        phone=raw.get("Phone") or None,
        is_active=bool(raw.get("IsActive", True)),
        entity_type=raw.get("Type") or None,
        created_at=_parse_xpm_datetime(raw["CreatedDate"]),
        modified_at=_parse_xpm_datetime(raw["ModifiedDate"]),
    )


def _parse_job(raw: dict[str, Any]) -> XPMJob:
    return XPMJob(
        id=str(raw["ID"]),
        name=raw.get("Name") or "",
        client_id=str(raw["ClientID"]),
        state=raw.get("State") or "",
        created_at=_parse_xpm_datetime(raw["StartDate"]),
        due_at=_parse_optional_datetime(raw.get("DueDate")),
        completed_at=_parse_optional_datetime(raw.get("CompletedDate")),
    )


def _parse_invoice(raw: dict[str, Any]) -> XPMInvoice:
    return XPMInvoice(
        id=str(raw["ID"]),
        number=str(raw.get("InvoiceNumber") or ""),
        client_id=str(raw["ClientID"]),
        total_amount=_parse_decimal(raw.get("TotalAmount")),
        total_tax=_parse_decimal(raw.get("TotalTax")),
        currency=raw.get("Currency") or "AUD",
        status=raw.get("Status") or "",
        issued_at=_parse_xpm_datetime(raw["Date"]),
        due_at=_parse_optional_datetime(raw.get("DueDate")),
    )


def _parse_relationship(raw: dict[str, Any]) -> XPMRelationship:
    return XPMRelationship(
        id=str(raw["ID"]),
        from_client_id=str(raw["FromClientID"]),
        to_client_id=str(raw["ToClientID"]),
        relationship_type=raw.get("Type") or "",
        is_active=bool(raw.get("IsActive", True)),
    )


def _parse_optional_datetime(value: Any) -> _dt.datetime | None:
    """Return None for missing/null, otherwise parse via _parse_xpm_datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return _parse_xpm_datetime(value)
    return None


def _parse_decimal(value: Any) -> Decimal:
    """Parse a money amount into ``Decimal``. None / empty -> 0."""
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, int | float):
        # str() preserves the literal more faithfully than Decimal(float)
        # which can introduce binary-float artefacts.
        return Decimal(str(value))
    return Decimal("0")
