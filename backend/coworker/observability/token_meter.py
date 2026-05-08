"""Per-firm/per-model/per-day token counters, stored in Redis.

Every successful Anthropic call records ``(input_tokens, output_tokens, +1
call)`` against a hash keyed by firm + model + UTC date. Counters live for
35 days (just over a calendar month, covering a typical reporting window
with a buffer). Phase 3H adds a nightly flush to Postgres for permanent
retention; until then this Redis hash is the only source of token usage
data, and that's deliberate — query latency under 1ms is non-negotiable
for the orchestrator's cost-guard logic.

Hash layout::

    HSET tokens:{firm_id}:{model}:{yyyy-mm-dd}
        input_tokens   <count>
        output_tokens  <count>
        calls          <count>
    EXPIRE tokens:{firm_id}:{model}:{yyyy-mm-dd} <35 days>

A hash (rather than three flat keys) keeps the TTL atomic — one EXPIRE
covers all three counters — and lets us add new fields later (cached
input tokens, thinking tokens, etc.) without changing the key schema.
"""
import datetime as _dt

from redis.asyncio import Redis

_DEFAULT_TTL_DAYS = 35


class TokenMeter:
    """Async Redis-backed token counter.

    Construct with a Redis client. Tests inject a client pointed at a
    dedicated logical DB; production constructs one from
    ``coworker.db.redis.get_redis``.
    """

    def __init__(self, redis: Redis, *, ttl_days: int = _DEFAULT_TTL_DAYS) -> None:
        if ttl_days <= 0:
            raise ValueError("ttl_days must be > 0")
        self._redis = redis
        self._ttl_seconds = ttl_days * 86400

    async def record(
        self,
        *,
        firm_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Increment today's counters for ``(firm_id, model)``.

        ``input_tokens`` and ``output_tokens`` may be zero (e.g. a
        count_tokens call) but never negative.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")

        key = self._key_for_today(firm_id=firm_id, model=model)
        pipe = self._redis.pipeline(transaction=False)
        pipe.hincrby(key, "input_tokens", input_tokens)
        pipe.hincrby(key, "output_tokens", output_tokens)
        pipe.hincrby(key, "calls", 1)
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def usage(
        self,
        *,
        firm_id: str,
        model: str,
        day: _dt.date | None = None,
    ) -> dict[str, int]:
        """Read counters for ``(firm_id, model, day)``.

        ``day`` defaults to today (UTC). Missing keys return zeros so
        callers can compose without checking for existence.
        """
        target = day or _dt.datetime.now(_dt.UTC).date()
        key = _key(firm_id=firm_id, model=model, day=target)
        # redis-py's hgetall typing is `Awaitable[dict] | dict` (a quirk
        # of its dual sync/async stubs); the runtime is always awaitable
        # in the asyncio.Redis client. The ignore is local and narrow.
        raw = await self._redis.hgetall(key)  # type: ignore[misc]
        return {
            "input_tokens": int(raw.get("input_tokens", 0)),
            "output_tokens": int(raw.get("output_tokens", 0)),
            "calls": int(raw.get("calls", 0)),
        }

    def _key_for_today(self, *, firm_id: str, model: str) -> str:
        return _key(
            firm_id=firm_id,
            model=model,
            day=_dt.datetime.now(_dt.UTC).date(),
        )


def _key(*, firm_id: str, model: str, day: _dt.date) -> str:
    return f"tokens:{firm_id}:{model}:{day.isoformat()}"
