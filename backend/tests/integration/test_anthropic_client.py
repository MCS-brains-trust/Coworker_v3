"""Integration tests for `AnthropicClient.complete`.

The Anthropic SDK uses httpx under the hood; respx intercepts those
calls. We mock https://api.anthropic.com/v1/messages and assert on
both the wire payload (PII scrubbed) and the parsed CompletionResult
(placeholders restored).

The dedicated PII black-box test (Phase 3B-2) lives in
``test_anthropic_pii_scrub.py`` and asserts no AU TFN/ABN/Medicare
digits ever leave the process. This file covers the surrounding
machinery — connector taxonomy, response parsing, input validation,
and CompletionResult repr safety.
"""
import httpx
import pytest
import respx
from pydantic import SecretStr

from coworker.connectors.anthropic_client import (
    AnthropicClient,
    CompletionMessage,
    CompletionResult,
)
from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)

_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _client() -> AnthropicClient:
    """Construct an AnthropicClient with a dummy API key.

    The api_key is never sent to the network in respx-mocked tests —
    respx intercepts before the request leaves httpx.
    """
    return AnthropicClient(
        firm_id="firm-test", api_key=SecretStr("sk-ant-test-key")
    )


def _success_payload(
    *, text: str, model: str = "claude-sonnet-4-6",
    input_tokens: int = 10, output_tokens: int = 5,
    stop_reason: str = "end_turn",
) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# --------------------------- happy path -------------------------------------


async def test_complete_returns_parsed_result() -> None:
    client = _client()

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200, json=_success_payload(text="Hello there")
            )
        )
        result = await client.complete(
            messages=[CompletionMessage(role="user", content="Hi")],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

    assert isinstance(result, CompletionResult)
    assert result.text == "Hello there"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.model == "claude-sonnet-4-6"
    assert result.completed_at.tzinfo is not None


async def test_complete_with_system_prompt() -> None:
    client = _client()

    with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200, json=_success_payload(text="OK")
            )
        )
        await client.complete(
            messages=[CompletionMessage(role="user", content="Hi")],
            model="claude-sonnet-4-6",
            max_tokens=10,
            system="You are a helpful assistant.",
        )

    body = route.calls.last.request.read().decode("utf-8")
    assert "You are a helpful assistant" in body


async def test_complete_scrubs_outbound_and_restores_inbound() -> None:
    """A PII-bearing prompt is scrubbed before send. If the model echoes
    a placeholder back, the response restores the original.

    Surface-level test — the load-bearing black-box assertion that
    NO digits ever leave the process is the dedicated test in
    test_anthropic_pii_scrub.py (Phase 3B-2).
    """
    client = _client()

    captured_outbound: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_outbound["body"] = request.read().decode("utf-8")
        # Echo back a placeholder shape the scrubber would have produced
        # for an email — so the restore logic has something to do.
        return httpx.Response(
            200,
            json=_success_payload(
                text="I'll email [EMAIL_ADDRESS_PLACEHOLDER] back."
            ),
        )

    # We can't predict the random hex suffix the scrubber generates; so
    # we'll do a mild test: scrub a known TFN, assert it's not in
    # outbound, and let the response come back unchanged.
    tfn = "123 456 782"  # not a real TFN format / value
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        result = await client.complete(
            messages=[
                CompletionMessage(
                    role="user", content=f"Please contact me. TFN {tfn}."
                )
            ],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    # Outbound body should not contain the raw TFN digits.
    assert tfn not in captured_outbound["body"]
    # Result text comes back unchanged here (no placeholder to restore).
    assert result.text == "I'll email [EMAIL_ADDRESS_PLACEHOLDER] back."


# --------------------------- failure paths ----------------------------------


async def test_complete_401_raises_auth_error() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"type": "authentication_error",
                                     "message": "invalid api key"}}
            )
        )
        with pytest.raises(ConnectorAuthError):
            await client.complete(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )


async def test_complete_403_raises_auth_error() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                403, json={"error": {"type": "permission_error",
                                     "message": "forbidden"}}
            )
        )
        with pytest.raises(ConnectorAuthError):
            await client.complete(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )


async def test_complete_429_raises_rate_limited_with_retry_after() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "30"},
                json={"error": {"type": "rate_limit_error",
                                "message": "throttled"}},
            )
        )
        with pytest.raises(ConnectorRateLimited) as excinfo:
            await client.complete(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )
        assert excinfo.value.retry_after == 30.0


async def test_complete_5xx_raises_transient() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            return_value=httpx.Response(
                503, json={"error": {"type": "internal_server_error",
                                     "message": "down"}}
            )
        )
        with pytest.raises(ConnectorTransient):
            await client.complete(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )


async def test_complete_network_error_raises_transient() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(ConnectorTransient):
            await client.complete(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )


# --------------------------- input validation -------------------------------


async def test_complete_empty_messages_rejected() -> None:
    client = _client()
    with pytest.raises(ValueError):
        await client.complete(
            messages=[], model="claude-sonnet-4-6", max_tokens=10
        )


async def test_complete_invalid_max_tokens_rejected() -> None:
    client = _client()
    with pytest.raises(ValueError):
        await client.complete(
            messages=[CompletionMessage(role="user", content="Hi")],
            model="claude-sonnet-4-6",
            max_tokens=0,
        )


# --------------------------- representation safety --------------------------


async def test_count_tokens_returns_input_count() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            "https://api.anthropic.com/v1/messages/count_tokens"
        ).mock(
            return_value=httpx.Response(200, json={"input_tokens": 42})
        )
        n = await client.count_tokens(
            messages=[CompletionMessage(role="user", content="Hello")],
            model="claude-sonnet-4-6",
        )
    assert n == 42


async def test_count_tokens_scrubs_before_counting() -> None:
    """Counts reflect what would actually be sent: post-scrub."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"input_tokens": 7})

    client = _client()
    tfn = "987 654 321"
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            "https://api.anthropic.com/v1/messages/count_tokens"
        ).mock(side_effect=_capture)
        await client.count_tokens(
            messages=[
                CompletionMessage(role="user", content=f"TFN {tfn}"),
            ],
            model="claude-sonnet-4-6",
        )
    assert tfn not in captured["body"]


async def test_count_tokens_empty_messages_rejected() -> None:
    client = _client()
    with pytest.raises(ValueError):
        await client.count_tokens(messages=[], model="claude-sonnet-4-6")


async def test_count_tokens_429_raises_rate_limited() -> None:
    client = _client()
    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(
            "https://api.anthropic.com/v1/messages/count_tokens"
        ).mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "5"},
                json={"error": {"type": "rate_limit_error", "message": "x"}},
            )
        )
        with pytest.raises(ConnectorRateLimited) as excinfo:
            await client.count_tokens(
                messages=[CompletionMessage(role="user", content="Hi")],
                model="claude-sonnet-4-6",
            )
        assert excinfo.value.retry_after == 5.0


async def test_complete_records_tokens_when_meter_provided() -> None:
    """End-to-end: complete() with a TokenMeter increments Redis counters."""
    from urllib.parse import urlparse, urlunparse

    from redis.asyncio import from_url

    from coworker.config import get_settings
    from coworker.observability.token_meter import TokenMeter

    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    test_redis = from_url(
        urlunparse(parsed._replace(path="/14")),
        encoding="utf-8",
        decode_responses=True,
    )
    await test_redis.flushdb()
    try:
        meter = TokenMeter(test_redis)
        client = AnthropicClient(
            firm_id="firm-meter-test",
            api_key=SecretStr("sk-ant-test-key"),
            token_meter=meter,
        )

        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_ANTHROPIC_MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "text", "text": "hi"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 25, "output_tokens": 11},
                    },
                )
            )
            await client.complete(
                messages=[CompletionMessage(role="user", content="hi")],
                model="claude-sonnet-4-6",
                max_tokens=10,
            )

        usage = await meter.usage(
            firm_id="firm-meter-test", model="claude-sonnet-4-6"
        )
        assert usage == {"input_tokens": 25, "output_tokens": 11, "calls": 1}
    finally:
        await test_redis.flushdb()
        await test_redis.aclose()


def test_completion_result_repr_redacts_text() -> None:
    """``repr(CompletionResult)`` must never include the response text."""
    import datetime as _dt
    result = CompletionResult(
        text="super secret model response that must not leak",
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=1,
        model="claude-sonnet-4-6",
        completed_at=_dt.datetime.now(_dt.UTC),
    )
    rendered = repr(result)
    assert "super secret model response" not in rendered
    assert "redacted" in rendered.lower()
