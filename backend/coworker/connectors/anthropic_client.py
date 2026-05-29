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
from collections.abc import AsyncIterator
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


@dataclass(frozen=True)
class StreamTextDelta:
    """One incremental text fragment from a streaming response.

    Text is in PII-placeholder space (i.e. whatever the model is
    emitting verbatim). Per-chunk placeholder restoration is not
    attempted because placeholders can straddle chunk boundaries;
    the final restored text arrives on ``StreamCompletion.full_text``.
    """

    text: str


@dataclass(frozen=True)
class StreamCompletion:
    """Terminal event emitted once the stream finishes successfully.

    ``full_text`` is the assembled response with PII placeholders
    restored — what consumers should persist as the canonical
    assistant message body. Token counts come from the SDK's final
    message ``usage`` payload.
    """

    full_text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str


@dataclass(frozen=True)
class StreamToolUseBlock:
    """A complete tool_use block surfaced after the streamed text deltas.

    Emitted by ``stream_message_with_tools`` once the stream's final
    message is available. ``input`` already has PII placeholders
    restored so the orchestrator can forward real values to the
    tool handler (which, in turn, will scrub again at its own
    Anthropic boundary if it makes another API call).
    """

    id: str
    name: str
    input: dict[str, Any]


StreamEvent = StreamTextDelta | StreamCompletion | StreamToolUseBlock


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
        thinking_budget: int | None = None,
    ) -> CompletionResult:
        """Send a completion to Anthropic; scrub on the way out, restore on return.

        Args:
            messages: conversation history. String content only.
            model: e.g. ``Settings.ANTHROPIC_MODEL_DEFAULT``. Never
                hardcode model strings at call sites.
            max_tokens: maximum response length.
            system: optional system prompt; scrubbed too.
            thinking_budget: opt-in extended thinking. When set, the
                model is allowed to spend up to ``thinking_budget``
                tokens reasoning before producing the visible
                response. Default 16000 in Settings; specialists
                (Phase 8) override to 32000. Pass ``None`` to disable
                thinking. The decision *when* to enable thinking is
                an orchestrator concern (Phase 5 has the
                auto-enable rules); this connector just forwards.

        Returns:
            ``CompletionResult`` with the response text (placeholders
            restored), stop reason, and token usage.

        Raises:
            ConnectorAuthError: 401 / 403 from Anthropic, or any
                other unrecoverable 4xx.
            ConnectorRateLimited: 429 from Anthropic. ``retry_after``
                is parsed from the Retry-After header where present.
            ConnectorTransient: 5xx, timeout, or network error.
            ValueError: ``messages`` is empty, ``max_tokens`` < 1, or
                ``thinking_budget`` < 1024 (Anthropic minimum).
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if thinking_budget is not None and thinking_budget < 1024:
            # Anthropic enforces a 1024-token minimum thinking budget
            # at the API layer. Validate locally so the error
            # surfaces as a clean ValueError rather than an opaque
            # 400 from the SDK with a vague message.
            raise ValueError(
                "thinking_budget must be >= 1024 (Anthropic minimum); "
                f"got {thinking_budget}"
            )
        if thinking_budget is not None and thinking_budget >= max_tokens:
            # The thinking budget eats into max_tokens; allowing
            # thinking_budget >= max_tokens leaves no room for the
            # actual response and produces empty output.
            raise ValueError(
                "thinking_budget must be strictly less than max_tokens "
                f"(got thinking_budget={thinking_budget}, max_tokens={max_tokens})"
            )

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
        if thinking_budget is not None:
            create_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }

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

    async def stream_message(
        self,
        messages: list[CompletionMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a Claude response chunk by chunk.

        Mirrors ``complete``'s contract for PII handling: messages
        and system prompt are scrubbed on the way out; placeholders
        are restored on the assembled full text returned in the
        terminal ``StreamCompletion`` event. The per-chunk
        ``StreamTextDelta`` payloads are left in placeholder space
        because a placeholder token can straddle a chunk boundary;
        consumers that want restored text should prefer
        ``StreamCompletion.full_text``.

        Args:
            messages: conversation history (string content only).
            model: model id; never hardcode at call sites.
            max_tokens: response cap.
            system: optional system prompt; scrubbed too.

        Yields:
            ``StreamTextDelta`` zero or more times, then exactly one
            ``StreamCompletion`` on success.

        Raises:
            ConnectorAuthError: 401/403/other unrecoverable 4xx.
            ConnectorRateLimited: 429, with parsed retry_after.
            ConnectorTransient: 5xx / timeout / network error.
            ValueError: empty messages or max_tokens < 1.
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")

        scrubbed_messages, mapping = self._scrub_messages(messages)
        scrubbed_system, system_mapping = self._scrub_system(system)
        if system_mapping:
            mapping.update(system_mapping)

        stream_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": scrubbed_messages,
        }
        if scrubbed_system is not None:
            stream_kwargs["system"] = scrubbed_system

        assembled: list[str] = []
        try:
            async with self._client.messages.stream(**stream_kwargs) as stream:
                async for chunk in stream.text_stream:
                    assembled.append(chunk)
                    yield StreamTextDelta(text=chunk)
                final_message = await stream.get_final_message()
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

        full_text = "".join(assembled)
        if mapping:
            for placeholder, original in mapping.items():
                full_text = full_text.replace(placeholder, original)

        if self._token_meter is not None:
            await self._token_meter.record(
                firm_id=self._firm_id,
                model=final_message.model,
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
            )

        yield StreamCompletion(
            full_text=full_text,
            stop_reason=final_message.stop_reason or "unknown",
            input_tokens=final_message.usage.input_tokens,
            output_tokens=final_message.usage.output_tokens,
            model=final_message.model,
        )

    async def stream_message_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a tool-use-capable Claude response.

        Mirrors ``complete_tool_use``'s PII contract: dict-shaped
        messages (mixed text / tool_use / tool_result content blocks)
        are scrubbed via ``_scrub_tool_use_messages``; the system
        prompt is scrubbed too. Tool definitions are passed through
        unscrubbed (today's definitions hold no PII; matches the
        existing ``complete_tool_use`` behaviour).

        Event ordering: zero or more ``StreamTextDelta`` (text in
        placeholder space, mid-stream), zero or more
        ``StreamToolUseBlock`` (after text stream finishes, derived
        from the final message — inputs have placeholders restored),
        then exactly one ``StreamCompletion`` (assembled full_text
        with placeholders restored, plus stop_reason and usage).

        Within one streamed call Claude emits text first then any
        tool_use blocks; we surface tool_uses after the text stream
        because the SDK only exposes them on the final message.
        Functionally fine — by the time the text preamble ends, the
        tool calls are ready to dispatch.

        Raises the same connector taxonomy as the other methods.
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")

        scrubbed_messages, mapping = self._scrub_tool_use_messages(messages)
        scrubbed_system, system_mapping = self._scrub_system(system)
        if system_mapping:
            mapping.update(system_mapping)

        stream_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": scrubbed_messages,
            "tools": tools,
        }
        if scrubbed_system is not None:
            stream_kwargs["system"] = scrubbed_system

        assembled: list[str] = []
        try:
            async with self._client.messages.stream(**stream_kwargs) as stream:
                async for chunk in stream.text_stream:
                    assembled.append(chunk)
                    yield StreamTextDelta(text=chunk)
                final_message = await stream.get_final_message()
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

        for block in final_message.content:
            if getattr(block, "type", None) == "tool_use":
                restored_input = _restore_in_value(
                    getattr(block, "input", {}) or {}, mapping
                )
                yield StreamToolUseBlock(
                    id=getattr(block, "id", ""),
                    name=getattr(block, "name", ""),
                    input=restored_input
                    if isinstance(restored_input, dict)
                    else {},
                )

        full_text = "".join(assembled)
        if mapping:
            for placeholder, original in mapping.items():
                full_text = full_text.replace(placeholder, original)

        if self._token_meter is not None:
            await self._token_meter.record(
                firm_id=self._firm_id,
                model=final_message.model,
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
            )

        yield StreamCompletion(
            full_text=full_text,
            stop_reason=final_message.stop_reason or "unknown",
            input_tokens=final_message.usage.input_tokens,
            output_tokens=final_message.usage.output_tokens,
            model=final_message.model,
        )

    async def complete_tool_use(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        thinking_budget: int | None = None,
    ) -> "ToolUseResult":
        """Tool-use completion for the orchestrator.

        Wraps the SDK's tool-use mode with:

        - PII scrubbing on every outgoing text and tool_result content
          payload. Tool_use blocks the model generated in earlier
          turns are passed through unchanged — they're either
          already in placeholder space (from when the user message
          was scrubbed) or carry no PII.
        - Placeholder restoration on incoming text and tool_use
          inputs so the engine's loop sees real data (handlers
          would otherwise receive placeholders and fail at the
          connector layer).
        - The same connector taxonomy as ``complete``
          (ConnectorAuthError / ConnectorRateLimited /
          ConnectorTransient).
        - Token metering when wired.

        Multi-iteration caveat: each call's mapping is local, so a
        single placeholder ``[EMAIL_001]`` in iteration 1 might
        appear as ``[EMAIL_005]`` in iteration 2 for the same
        email. The model handles this gracefully (Claude reads
        placeholders as opaque tokens), but reflection-heavy
        plugins that compare across iterations should normalise
        if precise mapping matters.

        Args:
            messages: list of Anthropic-shaped messages. Each
                message's content is either a string (user text)
                or a list of content blocks
                ({type: text/tool_use/tool_result}).
            system: optional system prompt.
            tools: Anthropic tool definitions
                (``ToolRegistry.to_anthropic_definitions()``).
            model: model id; never hardcode at callsites — read
                from Settings.
            max_tokens: response cap.
            thinking_budget: optional extended thinking budget.

        Returns:
            ``ToolUseResult`` with the response's content blocks
            (placeholders restored) plus stop_reason, tokens,
            and model.

        Raises:
            ConnectorAuthError: 401/403/other unrecoverable 4xx.
            ConnectorRateLimited: 429, with parsed retry_after.
            ConnectorTransient: 5xx / timeout / network error.
            ValueError: empty messages.
        """
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")

        scrubbed_messages, mapping = self._scrub_tool_use_messages(messages)
        scrubbed_system, system_mapping = self._scrub_system(system)
        if system_mapping:
            mapping.update(system_mapping)

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": scrubbed_messages,
            "tools": tools,
        }
        if scrubbed_system is not None:
            create_kwargs["system"] = scrubbed_system
        if thinking_budget is not None:
            if thinking_budget < 1024:
                raise ValueError(
                    "thinking_budget must be >= 1024 (Anthropic minimum); "
                    f"got {thinking_budget}"
                )
            if thinking_budget >= max_tokens:
                raise ValueError(
                    "thinking_budget must be strictly less than max_tokens "
                    f"(got thinking_budget={thinking_budget}, "
                    f"max_tokens={max_tokens})"
                )
            create_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }

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

        content_blocks = _content_blocks_to_dicts(response.content)
        if mapping:
            content_blocks = _restore_placeholders_in_blocks(
                content_blocks, mapping
            )

        if self._token_meter is not None:
            await self._token_meter.record(
                firm_id=self._firm_id,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        return ToolUseResult(
            content=content_blocks,
            stop_reason=response.stop_reason or "unknown",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )

    def _scrub_tool_use_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Scrub PII in a tool-use messages list.

        Walks each message's content. For string content (plain
        user text) the whole string is scrubbed. For list content
        (a mix of text / tool_use / tool_result blocks), each
        block's text fields are scrubbed individually:

        - ``text`` blocks: scrub the ``text`` field.
        - ``tool_use`` blocks: passed through unchanged. The model
          generated these from scrubbed inputs and they're already
          in placeholder space; touching them risks corrupting the
          tool dispatch.
        - ``tool_result`` blocks: scrub the ``content`` field
          (which contains real data from a tool handler's output).
          Both string and list shapes for content are handled.
        - Anything else: passed through unchanged.

        Returns (scrubbed_messages, merged_mapping) where mapping
        is the union of every per-call scrub's placeholder map.
        """
        merged: dict[str, str] = {}
        scrubbed_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if isinstance(content, str):
                result = self._scrubber.scrub(content)
                merged.update(result.mapping)
                scrubbed_messages.append({"role": role, "content": result.text})
                continue
            if isinstance(content, list):
                new_blocks: list[Any] = []
                for block in content:
                    new_blocks.append(self._scrub_block(block, merged))
                scrubbed_messages.append({"role": role, "content": new_blocks})
                continue
            # Unknown content shape — pass through.
            scrubbed_messages.append(msg)
        return scrubbed_messages, merged

    def _scrub_block(
        self, block: Any, mapping_accumulator: dict[str, str]
    ) -> Any:
        """Scrub PII in one content block. Mutates the accumulator."""
        if not isinstance(block, dict):
            return block
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                result = self._scrubber.scrub(text)
                mapping_accumulator.update(result.mapping)
                new_block = dict(block)
                new_block["text"] = result.text
                return new_block
            return block
        if block_type == "tool_result":
            new_block = dict(block)
            inner = new_block.get("content")
            if isinstance(inner, str) and inner:
                result = self._scrubber.scrub(inner)
                mapping_accumulator.update(result.mapping)
                new_block["content"] = result.text
            elif isinstance(inner, list):
                new_inner: list[Any] = []
                for sub in inner:
                    new_inner.append(
                        self._scrub_block(sub, mapping_accumulator)
                    )
                new_block["content"] = new_inner
            return new_block
        # tool_use / image / anything else: pass through.
        return block

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
    blocks are handled separately by ``complete_tool_use`` /
    ``_content_blocks_to_dicts``. Anything that isn't a recognised
    text block here is silently skipped — the tool-use path
    inspects the raw response separately when it needs that.
    """
    pieces: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            pieces.append(block.text)
    return "".join(pieces)


def _content_blocks_to_dicts(content: Any) -> list[dict[str, Any]]:
    """Convert an Anthropic SDK content list to dict-shaped blocks.

    The SDK returns ``TextBlock`` / ``ToolUseBlock`` (and similar)
    objects with attribute access. The orchestrator's engine
    expects plain dicts so the trace JSONB columns store the
    payload verbatim. Unknown block types are preserved with
    whatever attributes they expose plus their ``type``.
    """
    blocks: list[dict[str, Any]] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif block_type == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                }
            )
        elif block_type == "thinking":
            # Extended-thinking blocks carry the model's
            # internal reasoning. They're not scrubbed because
            # they only contain placeholders (the model thought
            # against scrubbed input) and aren't sent to tools.
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                }
            )
        else:
            # Fallback: a future block type. Preserve the type
            # field so callers can decide; the engine ignores
            # blocks it doesn't recognise.
            blocks.append({"type": block_type or "unknown"})
    return blocks


def _restore_placeholders_in_blocks(
    blocks: list[dict[str, Any]], mapping: dict[str, str]
) -> list[dict[str, Any]]:
    """Walk content blocks and restore PII placeholders.

    - ``text`` blocks: ``text`` field gets each placeholder
      replaced with its original value.
    - ``tool_use`` blocks: walk ``input`` recursively and
      replace placeholders in every string value. Lists and
      nested dicts are traversed; non-string values pass
      through unchanged.
    - Other block types pass through unchanged.

    Returns a new list; original blocks are not mutated.
    """
    if not mapping:
        return list(blocks)
    restored: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") == "text":
            new_block = dict(block)
            new_block["text"] = _restore_in_string(
                str(block.get("text", "")), mapping
            )
            restored.append(new_block)
        elif block.get("type") == "tool_use":
            new_block = dict(block)
            new_block["input"] = _restore_in_value(
                block.get("input", {}), mapping
            )
            restored.append(new_block)
        else:
            restored.append(block)
    return restored


def _restore_in_string(text: str, mapping: dict[str, str]) -> str:
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text


def _restore_in_value(value: Any, mapping: dict[str, str]) -> Any:
    """Recursively restore placeholders in any JSON-like value."""
    if isinstance(value, str):
        return _restore_in_string(value, mapping)
    if isinstance(value, list):
        return [_restore_in_value(item, mapping) for item in value]
    if isinstance(value, dict):
        return {
            key: _restore_in_value(sub, mapping)
            for key, sub in value.items()
        }
    return value


@dataclass(frozen=True)
class ToolUseResult:
    """Result of a single ``complete_tool_use`` call.

    Shape matches ``coworker.orchestrator.engine.ModelCallResult``
    so AnthropicClient.complete_tool_use is a drop-in ``ModelCaller``
    for the engine. ``content`` carries the response's content
    blocks as plain dicts with PII placeholders already restored;
    the engine writes them verbatim into the trace and consumes
    them for the loop's next iteration.
    """

    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str


def _parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        return None
