"""Microsoft Teams (incoming webhook) connector.

Scope is narrower than the other connectors. Teams has two
surfaces:

1. Legacy **incoming webhook URLs** — channel-specific URLs you POST
   a JSON payload to. One-way: a notification appears in the channel,
   no reply path. The URL is the credential (anyone with it can post)
   so it lives encrypted on ``firm.teams_webhook_url_ciphertext``.
2. **Graph chat API** — richer multi-direction conversations. Lives
   on the Graph connector under ``send_teams_message`` (see
   carry-forward note below).

This file covers (1). Used by Phase 11's principal-alert channel
(audit-chain tampering notifications) and any plugin that just needs
to drop a message into a configured Teams channel.

**Carry-forward from Phase 3C.** ``send_teams_message`` (Graph chat)
and ``get_user_profile`` were enumerated in the build plan §3C
read/write list but did not land in 3C-1 through 3C-6. They are not
needed by the Teams webhook surface and can land alongside the first
plugin that demands them (Phase 6 ``meeting_prep`` or Phase 11
``proactive_intelligence``).
"""
import datetime as _dt
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.shadow_mode import guard_writable
from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit
from coworker.security.encryption import decrypt_str

SYSTEM_ACTOR = "system"


class TeamsMessage(BaseModel):
    """A Teams incoming-webhook MessageCard payload.

    The minimum useful shape is just ``{"text": "..."}``; richer
    cards with sections, themeColor, and actions can be modelled
    here as use cases arise. For Phase 3 the text-only form covers
    the principal-alert use case.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    title: str | None = None
    theme_color: str | None = None  # hex without leading '#', e.g. "0078D4"


class TeamsClient:
    """Per-firm Teams webhook poster.

    Construct once per (firm, session) pair. The webhook URL is the
    only credential — decrypted from the firm row on each send so
    rotation through the onboarding wizard takes effect immediately
    without re-instantiating the client.
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

    async def send_message(self, message: TeamsMessage) -> None:
        """Post a MessageCard payload to the firm's Teams webhook.

        Shadow-mode guarded. A firm in shadow mode does not have its
        channel notified; ``guard_writable`` commits a
        ``shadow_blocked.teams.send_message`` audit row and raises.

        Args:
            message: ``TeamsMessage`` carrying the text (and optional
                title / theme colour).

        Raises:
            ShadowModeBlocked: firm.shadow_mode is True.
            ConnectorAuthError: webhook URL missing on the firm row,
                or Teams returned 401 / 403 / 410. (Microsoft returns
                410 Gone when a webhook has been deleted by the
                admin — same operational meaning as 401.)
            ConnectorRateLimited: 429.
            ConnectorTransient: 5xx / network error.
        """
        if not message.text:
            raise ValueError("message.text must be non-empty")

        firm_id_str = str(self._firm.id)
        action = "teams.send_message"
        # The text itself is not in audit — it may contain
        # client-sensitive content. We record that something was sent
        # plus its title (typically a topic) and length, which is
        # enough for principal review.
        extra: dict[str, Any] = {
            "title": message.title or "",
            "text_length": len(message.text),
        }

        await guard_writable(
            self._session,
            self._firm,
            action="teams.send_message",
            actor_type=self._actor_type,
            actor_id=self._actor_id,
        )

        webhook_url = self._require_webhook_url()
        payload = self._build_payload(message)

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.RequestError as exc:
            await self._audit_failure(
                action=action, reason="network_error", extra=extra
            )
            raise ConnectorTransient(
                "network error talking to Teams webhook"
            ) from exc

        await self._raise_for_teams_status(
            response, action=action, extra=extra
        )

        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action=action,
            payload={
                "user_id": self._actor_id,
                "title": message.title or "",
                "text_length": len(message.text),
                "sent_at": _dt.datetime.now(_dt.UTC).isoformat(
                    timespec="seconds"
                ),
            },
        )
        await self._session.commit()

    def _require_webhook_url(self) -> str:
        """Decrypt the firm's Teams webhook URL; raise if missing."""
        if self._firm.teams_webhook_url_ciphertext is None:
            raise ConnectorAuthError(
                f"firm {self._firm.id} has no teams_webhook_url; "
                "Teams not connected"
            )
        try:
            return decrypt_str(
                self._firm.teams_webhook_url_ciphertext,
                firm_id=str(self._firm.id),
            )
        except Exception as exc:
            raise ConnectorAuthError(
                f"firm {self._firm.id} teams_webhook_url ciphertext "
                "could not be decrypted"
            ) from exc

    def _build_payload(self, message: TeamsMessage) -> dict[str, Any]:
        """Assemble the MessageCard JSON body Teams expects."""
        payload: dict[str, Any] = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "text": message.text,
        }
        if message.title:
            payload["title"] = message.title
        if message.theme_color:
            payload["themeColor"] = message.theme_color
        return payload

    async def _audit_failure(
        self,
        *,
        action: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append ``<action>_failed`` and commit."""
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

    async def _raise_for_teams_status(
        self,
        response: httpx.Response,
        *,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Audit + raise for non-2xx Teams responses.

        Teams webhooks have a simpler vocabulary than the data APIs:
        no 404 (the URL is the credential, and bad URLs return 401 /
        400), and 410 means the webhook was deleted by the admin —
        we lump that with auth failures because the operational
        remediation is the same (re-issue the URL via onboarding).
        """
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403, 410, 404):
            await self._audit_failure(
                action=action, reason=f"teams_{status}", extra=extra
            )
            raise ConnectorAuthError(
                f"Teams rejected webhook post: HTTP {status}"
            )
        if status == 429:
            retry_after = _parse_retry_after(
                response.headers.get("Retry-After")
            )
            await self._audit_failure(
                action=action, reason="teams_429", extra=extra
            )
            raise ConnectorRateLimited(retry_after=retry_after)
        if 500 <= status < 600:
            await self._audit_failure(
                action=action, reason="teams_5xx", extra=extra
            )
            raise ConnectorTransient(f"Teams returned {status}")

        await self._audit_failure(
            action=action, reason=f"teams_{status}", extra=extra
        )
        raise ConnectorAuthError(f"Teams returned {status}")


def _parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None
