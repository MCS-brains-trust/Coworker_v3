"""Anthropic API connector — the only place in the codebase that talks
to api.anthropic.com.

Every other model call goes through this client. It enforces three
non-negotiable properties at the boundary:

1. **PII scrubbing on every prompt.** The Australian recogniser set
   in ``coworker.security.pii`` runs on every message and the system
   prompt before they leave the process. Placeholders are restored
   in the response text so consumers see the originals back. The
   black-box test in ``test_anthropic_pii_scrub.py`` (Phase 3B-2)
   asserts that no TFN / ABN / Medicare digits ever appear in the
   outbound HTTP body.

2. **Per-firm scope.** The client is constructed with a ``firm_id``
   so future enhancements (BYO Anthropic keys per firm, per-firm
   budget tracking) plug in cleanly. Today the platform-default key
   from ``Settings`` is used unless an explicit ``api_key`` is passed.

3. **Connector taxonomy.** The Anthropic SDK's exception family is
   normalised to ``ConnectorAuthError`` / ``ConnectorRateLimited`` /
   ``ConnectorTransient`` so callers can reason about failure modes
   uniformly across Graph, Anthropic, XPM, FuseSign, Teams.

Token metering and extended-thinking opt-in land in subsequent
sub-phase commits (3B-4 and 3B-5 respectively).
"""
import datetime as _dt
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
from pydantic import SecretStr

from coworker.config import get_settings
from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.observability.token_meter import TokenMeter
from coworker.security.pii import PIIScrubber, ScrubResult

# Module-level lazy singleton. PIIScrubber is expensive to initialise
# (loads a spaCy model, ~2s on dev hardware). Sharing one instance
# across all AnthropicClient calls keeps the cost amortised; tests
# that need an isolated scrubber can pass one to the constructor.
_pii_scrubber: PIIScrubber | None = None


def _default_scrubber() -> PIIScrubber:
    global _pii_scrubber
    if _pii_scrubber is None:
        _pii_scrubber = PIIScrubber()
    return _pii_scrubber


CompletionRole = Literal["user", "assistant"]


@dataclass(frozen=True)
class CompletionMessage:
    """A single message in a completion call. String content only.

    Block content (tool_use, tool_result, image) lands in Phase 3B-5
    or Phase 5 alongside the orchestrator's tool dispatch.
    """

    role: CompletionRole
    content: str


@dataclass(frozen=True, repr=False)
class CompletionResult:
    """The result of a successful completion call.

    Frozen so callers cannot mutate fields. Custom ``__repr__``
    redacts the rendered text — accidentally logging a result object
    must not leak Anthropic's response. The text itself is the
    legitimate consumer of this field; explicit access (``result.text``)
    is fine.
    """

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    completed_at: _dt.datetime

    def __repr__(self) -> str:
        return (
            f"CompletionResult(model={self.model!r}, "
            f"stop_reason={self.stop_reason!r}, "
            f"input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, "
            f"text=<{len(self.text)} chars redacted>)"
        )


class AnthropicClient:
    """Per-firm Anthropic client with PII scrubbing and connector taxonomy.

    Construct once per firm, reuse across calls. Constructor is cheap;
    the SDK's underlying httpx client is lazy.
    """

    def __init__(
        self,
        firm_id: str,
        *,
        api_key: SecretStr | None = None,
        scrubber: PIIScrubber | None = None,
        token_meter: TokenMeter | None = None,
    ) -> None:
        self._firm_id = firm_id
        if api_key is None:
            api_key = get_settings().ANTHROPIC_API_KEY
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value()
        )
        self._scrubber = scrubber if scrubber is not None else _default_scrubber()
        # token_meter is optional. Production wiring (Phase 5
        # orchestrator) constructs every AnthropicClient with a
        # TokenMeter so usage is recorded; tests that don't need
        # metering pass None (the default) and the recording call
        # is skipped. The architecture's "every Anthropic call meters
        # tokens" guarantee is therefore enforced by the orchestrator
        # construction site, not by this constructor.
        self._token_meter = token_meter

    @property
    def firm_id(self) -> str:
        return self._firm_id

    async def count_tokens(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        system: str | None = None,
    ) -> int:
        """Count input tokens for a hypothetical ``complete`` call.

        Returns the input-side token count *after* PII scrubbing — so
        the answer reflects what would actually go on the wire, not
        the raw input. The Phase 5 orchestrator uses this for its
        per-context cost guard: estimate token cost before sending,
        decline (and queue for approval) if the budget is exceeded.

        Raises the same connector taxonomy as ``complete``.
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")

        scrubbed_messages, _ = self._scrub_messages(messages)
        scrubbed_system, _ = self._scrub_system(system)

        count_kwargs: dict[str, Any] = {
            "model": model,
            "messages": scrubbed_messages,
        }
        if scrubbed_system is not None:
            count_kwargs["system"] = scrubbed_system

        try:
            result = await self._client.messages.count_tokens(**count_kwargs)
        except anthropic.AuthenticationError as exc:
            raise ConnectorAuthError(
                f"Anthropic auth failed: {exc.message}"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ConnectorAuthError(
                f"Anthropic permission denied: {exc.message}"
            ) from exc
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after(
                exc.response.headers.get("Retry-After")
            )
            raise ConnectorRateLimited(retry_after=retry_after) from exc
        except anthropic.APIStatusError as exc:
            if 500 <= exc.status_code < 600:
                raise ConnectorTransient(
                    f"Anthropic returned {exc.status_code}: {exc.message}"
                ) from exc
            raise ConnectorAuthError(
                f"Anthropic returned {exc.status_code}: {exc.message}"
            ) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise ConnectorTransient(
                f"network/timeout talking to Anthropic: {exc}"
            ) from exc

        return int(result.input_tokens)

    async def complete(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
    ) -> CompletionResult:
        """Send a completion to Anthropic; scrub on the way out, restore on return.

        Args:
            messages: conversation history. String content only.
            model: e.g. ``Settings.ANTHROPIC_MODEL_DEFAULT``. Never
                hardcode model strings at call sites.
            max_tokens: maximum response length.
            system: optional system prompt; scrubbed too.

        Returns:
            ``CompletionResult`` with the response text (placeholders
            restored), stop reason, and token usage.

        Raises:
            ConnectorAuthError: 401 / 403 from Anthropic, or any
                other unrecoverable 4xx.
            ConnectorRateLimited: 429 from Anthropic. ``retry_after``
                is parsed from the Retry-After header where present.
            ConnectorTransient: 5xx, timeout, or network error.
            ValueError: ``messages`` is empty or ``max_tokens`` < 1.
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")

        scrubbed_messages, mapping = self._scrub_messages(messages)
        scrubbed_system, system_mapping = self._scrub_system(system)
        if system_mapping:
            mapping.update(system_mapping)

        # Build kwargs so we only pass `system` when it's actually set;
        # avoids the SDK's NOT_GIVEN sentinel which mypy doesn't accept
        # against the typed signature in newer SDK versions.
        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": scrubbed_messages,
        }
        if scrubbed_system is not None:
            create_kwargs["system"] = scrubbed_system

        try:
            response = await self._client.messages.create(**create_kwargs)
        except anthropic.AuthenticationError as exc:
            raise ConnectorAuthError(
                f"Anthropic auth failed: {exc.message}"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ConnectorAuthError(
                f"Anthropic permission denied: {exc.message}"
            ) from exc
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
            raise ConnectorRateLimited(retry_after=retry_after) from exc
        except anthropic.APIStatusError as exc:
            # Catches BadRequestError, NotFoundError, etc plus 5xx.
            if 500 <= exc.status_code < 600:
                raise ConnectorTransient(
                    f"Anthropic returned {exc.status_code}: {exc.message}"
                ) from exc
            raise ConnectorAuthError(
                f"Anthropic returned {exc.status_code}: {exc.message}"
            ) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise ConnectorTransient(
                f"network/timeout talking to Anthropic: {exc}"
            ) from exc

        rendered = _render_text(response.content)
        # Restore placeholders so consumers see the original PII back.
        if mapping:
            for placeholder, original in mapping.items():
                rendered = rendered.replace(placeholder, original)

        if self._token_meter is not None:
            await self._token_meter.record(
                firm_id=self._firm_id,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        return CompletionResult(
            text=rendered,
            stop_reason=response.stop_reason or "unknown",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            completed_at=_dt.datetime.now(_dt.UTC),
        )

    def _scrub_messages(
        self, messages: list[CompletionMessage]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Scrub each message's content; return (sdk-shaped messages, merged mapping)."""
        merged_mapping: dict[str, str] = {}
        scrubbed: list[dict[str, Any]] = []
        for msg in messages:
            result = self._scrubber.scrub(msg.content)
            merged_mapping.update(result.mapping)
            scrubbed.append({"role": msg.role, "content": result.text})
        return scrubbed, merged_mapping

    def _scrub_system(
        self, system: str | None
    ) -> tuple[str | None, dict[str, str]]:
        if system is None:
            return None, {}
        result: ScrubResult = self._scrubber.scrub(system)
        return result.text, result.mapping


def _render_text(content: Any) -> str:
    """Concatenate text blocks from an Anthropic Message response.

    Phase 3B-1 only handles ``text`` blocks. Tool-use / tool-result
    blocks land in Phase 5 alongside the orchestrator. Anything that
    isn't a recognised text block is silently skipped — the orchestrator
    will inspect the raw response separately when it needs that.
    """
    pieces: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            pieces.append(block.text)
    return "".join(pieces)


def _parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None
