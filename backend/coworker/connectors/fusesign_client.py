"""FuseSign connector — digital signatures for accounting documents.

Single per-firm class. The only code path in this codebase that talks
to ``api.fusesign.com``. Used by Phase 6's ``engagement_letter`` and
``fusesign_monitor`` plugins.

Authentication: long-lived API key issued by the firm's FuseSign
admin, stored encrypted in ``firm.fusesign_api_key_ciphertext``. Sent
in the ``X-API-Key`` header on every request. No refresh logic — the
key rotates only when the admin regenerates it through FuseSign's UI,
which goes through the onboarding wizard.

Audit prefix: ``fusesign.*``. Status-to-exception mapping uses
``fusesign_*`` reason strings.

The exact endpoint paths below are based on the documented FuseSign
v1 REST API and may need adjustment during Phase 16A shadow testing.
They're centralised as module constants so any drift is fixed in one
place.

This commit (3F-1) lands the read surface — ``list_envelopes`` and
``get_envelope``. The write methods (``create_envelope``,
``send_reminder``, ``register_webhook``) follow in 3F-2 with the
shadow-mode guard.
"""
import datetime as _dt
from typing import Any, Literal
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.shadow_mode import guard_writable
from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit
from coworker.security.encryption import decrypt_str

_API_BASE = "https://api.fusesign.com/v1"
_ENVELOPES_PATH = "envelopes"
_REMINDERS_PATH = "reminders"  # POST /envelopes/{id}/reminders
_WEBHOOKS_PATH = "webhooks"

SYSTEM_ACTOR = "system"


class FuseSignRecipient(BaseModel):
    """One signer / viewer / approver on a FuseSign envelope."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    email: str
    role: str  # "signer", "viewer", "approver" — free-text from FuseSign
    status: str  # "pending", "signed", "declined", "viewed", etc.


class FuseSignEnvelope(BaseModel):
    """A FuseSign envelope (the unit of signature workflow).

    One envelope contains one or more documents and one or more
    recipients. The envelope as a whole has a status (the rollup of
    all recipients) and the recipients have individual statuses.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    # Free-text. Common values: "draft", "sent", "viewed", "signed",
    # "declined", "expired", "voided". Not constrained to a Literal
    # because FuseSign may add states without breaking us.
    status: str
    document_count: int = 0
    recipients: list[FuseSignRecipient] = []
    created_at: _dt.datetime
    updated_at: _dt.datetime


class CreateEnvelopeRecipient(BaseModel):
    """One recipient to include when creating a FuseSign envelope."""

    model_config = ConfigDict(frozen=True)

    name: str
    email: str
    role: str = "signer"  # "signer" / "viewer" / "approver"


class CreateEnvelopeDocument(BaseModel):
    """One document to upload when creating a FuseSign envelope.

    ``content_base64`` is the document's bytes encoded as base64. The
    caller is responsible for the encoding so the connector layer
    stays free of file-system decisions — vision-pipeline output, KB
    template renders, and ad-hoc uploads all go through the same path.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    content_base64: str


class FuseSignClient:
    """Per-firm FuseSign REST client.

    Construct once per (firm, session) pair. ``actor_id`` /
    ``actor_type`` default to ``"system"`` because the typical caller
    is the ``fusesign_monitor`` plugin running on a schedule; user-
    initiated paths can pass user identifiers.
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

    async def list_envelopes(
        self, *, status: str | None = None
    ) -> list[FuseSignEnvelope]:
        """List envelopes for the firm, optionally filtered by status.

        Args:
            status: optional FuseSign status string. Common values
                are "draft", "sent", "viewed", "signed", "declined",
                "expired", "voided"; passed through verbatim so any
                future FuseSign status works.

        Returns:
            One page of envelopes. Pagination beyond a single page
            lands when we have a tenant with hundreds of in-flight
            envelopes; Phase 3 firms are below that threshold.
        """
        action = "fusesign.envelopes.list"
        url = f"{_API_BASE}/{_ENVELOPES_PATH}"
        params: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if status is not None:
            if not status:
                raise ValueError("status must be non-empty when provided")
            params["status"] = status
            extra["status"] = status

        response = await self._authenticated_get(
            url=url, params=params, action=action, extra=extra,
        )
        raw_items = _extract_envelopes_collection(response.json())
        envelopes = [_parse_envelope(item) for item in raw_items]

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "count": len(envelopes),
                **extra,
            },
        )
        await self._session.commit()
        return envelopes

    async def get_envelope(self, envelope_id: str) -> FuseSignEnvelope:
        """Fetch one envelope by id.

        Raises:
            ConnectorNotFound: 404 (envelope voided or never existed).
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: ``envelope_id`` is empty.
        """
        if not envelope_id:
            raise ValueError("envelope_id must be non-empty")

        action = "fusesign.envelopes.get"
        url = (
            f"{_API_BASE}/{_ENVELOPES_PATH}/{quote(envelope_id, safe='')}"
        )
        extra: dict[str, Any] = {"envelope_id": envelope_id}

        response = await self._authenticated_get(
            url=url, params=None, action=action, extra=extra,
            allow_not_found=True,
        )
        envelope = _parse_envelope(_extract_envelope_single(response.json()))

        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "envelope_id": envelope_id,
            },
        )
        await self._session.commit()
        return envelope

    async def create_envelope(
        self,
        *,
        name: str,
        recipients: list[CreateEnvelopeRecipient],
        documents: list[CreateEnvelopeDocument],
    ) -> FuseSignEnvelope:
        """Create a new FuseSign envelope.

        Shadow-mode guarded. Returns the created envelope so callers
        can persist its id and surface ``web_link``-equivalent fields.

        Raises:
            ShadowModeBlocked: firm.shadow_mode is True.
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: empty ``name`` / ``recipients`` / ``documents``
                or any malformed entry.
        """
        if not name:
            raise ValueError("name must be non-empty")
        if not recipients:
            raise ValueError("recipients must not be empty")
        if not documents:
            raise ValueError("documents must not be empty")

        firm_id_str = str(self._firm.id)
        action = "fusesign.envelopes.create"
        extra: dict[str, Any] = {
            "name": name,
            "recipient_count": len(recipients),
            "document_count": len(documents),
        }

        await guard_writable(
            self._session,
            self._firm,
            action="fusesign.create_envelope",
            actor_type=self._actor_type,
            actor_id=self._actor_id,
        )

        # Sandbox rerouting (pre-pilot Task 1). Every signer email
        # gets replaced with the firm's catchall; names are
        # preserved so the principal can still see who the
        # envelope was meant for. Fires even with shadow_mode=False.
        recipients = _maybe_sandbox_reroute_recipients(
            self._firm, recipients,
        )

        payload = {
            "name": name,
            "recipients": [
                {"name": r.name, "email": r.email, "role": r.role}
                for r in recipients
            ],
            "documents": [
                {"name": d.name, "content_base64": d.content_base64}
                for d in documents
            ],
        }
        url = f"{_API_BASE}/{_ENVELOPES_PATH}"
        response = await self._authenticated_post(
            url=url, json=payload, action=action, extra=extra,
            allow_not_found=False,
        )
        envelope = _parse_envelope(_extract_envelope_single(response.json()))

        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "envelope_id": envelope.id,
                "recipient_count": len(recipients),
                "document_count": len(documents),
                # name lands in audit so principals reviewing shadow-
                # mode log can see what would have been created. Name
                # is typically the document title ("Engagement Letter
                # — Acme Pty Ltd"), not PII-sensitive in itself.
                "name": name,
            },
        )
        await self._session.commit()
        return envelope

    async def send_reminder(self, envelope_id: str) -> None:
        """Trigger a FuseSign reminder email to outstanding signers.

        Shadow-mode guarded — the reminder is an outbound email,
        which counts as a firm-data side effect.
        """
        if not envelope_id:
            raise ValueError("envelope_id must be non-empty")

        firm_id_str = str(self._firm.id)
        action = "fusesign.envelopes.send_reminder"
        extra: dict[str, Any] = {"envelope_id": envelope_id}

        await guard_writable(
            self._session,
            self._firm,
            action="fusesign.send_reminder",
            actor_type=self._actor_type,
            actor_id=self._actor_id,
        )

        # Sandbox no-op (pre-pilot Task 1). The original envelope's
        # signer is already the catchall; another reminder doesn't
        # add signal. Quiet INFO log + no Graph call.
        if self._firm.is_sandbox:
            logger.info(
                "sandbox fusesign send_reminder no-op firm_id={} "
                "envelope_id={}",
                self._firm.id, envelope_id,
            )
            return

        url = (
            f"{_API_BASE}/{_ENVELOPES_PATH}/"
            f"{quote(envelope_id, safe='')}/{_REMINDERS_PATH}"
        )
        await self._authenticated_post(
            url=url, json={}, action=action, extra=extra,
            allow_not_found=True,
        )

        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "envelope_id": envelope_id,
            },
        )
        await self._session.commit()

    async def register_webhook(self, target_url: str) -> str:
        """Register a webhook for envelope state changes.

        Not shadow-guarded: webhook registration is observation
        infrastructure, not a firm-data write. A shadow-mode firm
        still needs FuseSign events flowing in so the system can
        prepare (would-be) follow-up actions; the actions themselves
        stay blocked at create_envelope / send_reminder.

        Returns:
            The new webhook's id.

        Raises:
            ConnectorAuthError, ConnectorRateLimited, ConnectorTransient.
            ValueError: empty ``target_url``.
        """
        if not target_url:
            raise ValueError("target_url must be non-empty")

        firm_id_str = str(self._firm.id)
        action = "fusesign.webhooks.register"
        extra: dict[str, Any] = {"target_url": target_url}

        url = f"{_API_BASE}/{_WEBHOOKS_PATH}"
        response = await self._authenticated_post(
            url=url,
            json={"url": target_url},
            action=action,
            extra=extra,
            allow_not_found=False,
        )
        raw = response.json()
        webhook_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if not webhook_id:
            await self._audit_failure(
                action=action,
                reason="fusesign_missing_id",
                extra=extra,
            )
            raise ConnectorTransient(
                "FuseSign register_webhook returned no webhook ID"
            )

        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "webhook_id": webhook_id,
                "target_url": target_url,
            },
        )
        await self._session.commit()
        return webhook_id

    async def _authenticated_post(
        self,
        *,
        url: str,
        json: dict[str, Any],
        action: str,
        extra: dict[str, Any],
        allow_not_found: bool,
    ) -> httpx.Response:
        """POST JSON with API-key auth; audit + raise on error."""
        api_key = self._require_api_key()
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    url,
                    json=json,
                    headers={
                        **self._auth_headers(api_key),
                        "Content-Type": "application/json",
                    },
                )
        except httpx.RequestError as exc:
            await self._audit_failure(
                action=action, reason="network_error", extra=extra
            )
            raise ConnectorTransient(
                "network error talking to FuseSign"
            ) from exc

        await self._raise_for_fusesign_status(
            response,
            action=action,
            allow_not_found=allow_not_found,
            extra=extra,
        )
        return response

    async def _authenticated_get(
        self,
        *,
        url: str,
        params: dict[str, Any] | None,
        action: str,
        extra: dict[str, Any],
        allow_not_found: bool = False,
    ) -> httpx.Response:
        """GET with API-key auth; audit + raise on error."""
        api_key = self._require_api_key()
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    url,
                    params=params,
                    headers=self._auth_headers(api_key),
                )
        except httpx.RequestError as exc:
            await self._audit_failure(
                action=action, reason="network_error", extra=extra
            )
            raise ConnectorTransient(
                "network error talking to FuseSign"
            ) from exc

        await self._raise_for_fusesign_status(
            response,
            action=action,
            allow_not_found=allow_not_found,
            extra=extra,
        )
        return response

    def _require_api_key(self) -> str:
        """Decrypt the FuseSign API key; raise ConnectorAuthError if missing."""
        if self._firm.fusesign_api_key_ciphertext is None:
            raise ConnectorAuthError(
                f"firm {self._firm.id} has no fusesign_api_key; "
                "FuseSign not connected"
            )
        try:
            return decrypt_str(
                self._firm.fusesign_api_key_ciphertext,
                firm_id=str(self._firm.id),
            )
        except Exception as exc:
            raise ConnectorAuthError(
                f"firm {self._firm.id} fusesign_api_key ciphertext "
                "could not be decrypted"
            ) from exc

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {
            "X-API-Key": api_key,
            "Accept": "application/json",
        }

    async def _audit_failure(
        self,
        *,
        action: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append ``<action>_failed`` for any FuseSign operation and commit.

        Committed inline so the audit row survives caller rollback —
        same pattern as the XPM and Graph helpers.
        """
        payload: dict[str, Any] = {
            "user_id": self._actor_id,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        await append_audit(
            self._session,
            firm_id=str(self._firm.id),
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=f"{action}_failed",
            payload=payload,
        )
        await self._session.commit()

    async def _raise_for_fusesign_status(
        self,
        response: httpx.Response,
        *,
        action: str,
        allow_not_found: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Audit + raise the right ConnectorError for a non-2xx FuseSign response.

        Same shape as Graph's and XPM's helpers but with ``fusesign_*``
        reason prefixes so audit-log readers can filter by connector.
        """
        status = response.status_code
        if 200 <= status < 300:
            return

        if status == 404 and allow_not_found:
            await self._audit_failure(
                action=action, reason="fusesign_404", extra=extra
            )
            raise ConnectorNotFound(
                f"FuseSign returned 404 for {action}"
            )
        if status == 401 or status == 403:
            await self._audit_failure(
                action=action, reason=f"fusesign_{status}", extra=extra
            )
            raise ConnectorAuthError(
                f"FuseSign rejected request: HTTP {status}"
            )
        if status == 429:
            retry_after = _parse_retry_after(
                response.headers.get("Retry-After")
            )
            await self._audit_failure(
                action=action, reason="fusesign_429", extra=extra
            )
            raise ConnectorRateLimited(retry_after=retry_after)
        if 500 <= status < 600:
            await self._audit_failure(
                action=action, reason="fusesign_5xx", extra=extra
            )
            raise ConnectorTransient(f"FuseSign returned {status}")

        await self._audit_failure(
            action=action, reason=f"fusesign_{status}", extra=extra
        )
        raise ConnectorAuthError(f"FuseSign returned {status}")


def _parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None


def _parse_fusesign_datetime(value: str) -> _dt.datetime:
    """Parse FuseSign ISO-8601 timestamp into tz-aware datetime.

    FuseSign returns ISO-8601 with ``Z`` suffix. We normalise to
    ``+00:00`` for fromisoformat and treat any naive output as UTC.
    """
    parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _extract_envelopes_collection(body: Any) -> list[dict[str, Any]]:
    """Pull a list of envelopes out of FuseSign's response shape.

    Accepts ``{"envelopes": [...]}``, ``{"data": [...]}`` or a raw
    list. Other shapes return ``[]``.
    """
    if isinstance(body, list):
        items_list: list[dict[str, Any]] = body
        return items_list
    if isinstance(body, dict):
        for key in ("envelopes", "data"):
            value = body.get(key)
            if isinstance(value, list):
                top_items: list[dict[str, Any]] = value
                return top_items
    return []


def _extract_envelope_single(body: Any) -> dict[str, Any]:
    """Pull a single envelope from a get_envelope response.

    Accepts ``{"envelope": {...}}``, ``{"data": {...}}`` or the bare
    object. Raises ``ValueError`` for unexpected shapes (which would
    indicate a real upstream issue worth surfacing rather than
    silently producing an empty model).
    """
    if not isinstance(body, dict):
        raise ValueError(
            "FuseSign returned non-object body for get_envelope"
        )
    for key in ("envelope", "data"):
        value = body.get(key)
        if isinstance(value, dict):
            inner: dict[str, Any] = value
            return inner
    return body


def _parse_envelope(raw: dict[str, Any]) -> FuseSignEnvelope:
    raw_recipients = raw.get("recipients") or []
    recipients = [
        _parse_recipient(r)
        for r in raw_recipients
        if isinstance(r, dict) and r.get("email")
    ]

    documents = raw.get("documents") or []
    doc_count = (
        len(documents) if isinstance(documents, list)
        else int(raw.get("document_count") or 0)
    )

    return FuseSignEnvelope(
        id=str(raw["id"]),
        name=raw.get("name") or "",
        status=raw.get("status") or "",
        document_count=doc_count,
        recipients=recipients,
        created_at=_parse_fusesign_datetime(raw["created_at"]),
        updated_at=_parse_fusesign_datetime(raw["updated_at"]),
    )


def _parse_recipient(raw: dict[str, Any]) -> FuseSignRecipient:
    return FuseSignRecipient(
        id=str(raw["id"]),
        name=raw.get("name") or "",
        email=raw["email"],
        role=raw.get("role") or "",
        status=raw.get("status") or "",
    )


def _maybe_sandbox_reroute_recipients(
    firm: Firm,
    recipients: list[CreateEnvelopeRecipient],
) -> list[CreateEnvelopeRecipient]:
    """Apply sandbox firm rerouting (pre-pilot Task 1).

    Replaces every recipient's email with the firm's
    ``sandbox_outbound_catchall``; preserves name + role so the
    principal can still see who the envelope was intended for in
    the principal-side audit trail. Non-sandbox firms get the
    list back unchanged.

    The migration's CHECK constraint guarantees the catchall is
    set whenever is_sandbox=True; the assert is defence-in-depth.
    """
    if not firm.is_sandbox:
        return recipients
    assert firm.sandbox_outbound_catchall is not None, (
        "sandbox firm has no catchall — CHECK constraint should "
        "have prevented this; firm_id=" + str(firm.id)
    )
    catchall = firm.sandbox_outbound_catchall
    logger.info(
        "sandbox fusesign rewrite firm_id={} count={} catchall={!r}",
        firm.id, len(recipients), catchall,
    )
    return [
        CreateEnvelopeRecipient(
            name=r.name, email=catchall, role=r.role,
        )
        for r in recipients
    ]
