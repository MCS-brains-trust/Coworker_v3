"""Per-firm/per-model/per-day token counters, stored in Redis.

Every successful Anthropic call records ``(input_tokens, output_tokens, +1
call)`` against a hash keyed by firm + model + UTC date. Counters live for
35 days (just over a calendar month, covering a typical reporting window
with a buffer). Phase 3H-1 added a ``token_usage`` table for permanent
retention; ``flush_token_meter_to_postgres`` (below) copies the live
Redis hashes into it. Phase 6's APScheduler will call this nightly;
the Phase 3H-3 CLI calls it on demand before producing a report.

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
import uuid

from redis.asyncio import Redis
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.db.models.token_usage import TokenUsageRow
from coworker.db.session import firm_context

_DEFAULT_TTL_DAYS = 35
_SCAN_BATCH = 200


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


async def flush_token_meter_to_postgres(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Read every ``tokens:*`` Redis hash and UPSERT into ``token_usage``.

    Idempotent for re-runs: ``ON CONFLICT (firm_id, model, day) DO
    UPDATE`` writes the live counters, so a re-flush mid-day picks
    up additional records since the last flush. The Postgres value
    always reflects the current Redis hash, not a sum across flushes.

    Each row is committed in its own per-firm session under
    ``firm_context``. RLS on ``token_usage`` requires
    ``app.firm_id`` to match the row's firm_id at INSERT time;
    opening a fresh session per row makes that contract explicit and
    keeps the transactions tight (a single bad firm row doesn't roll
    back hours of accumulated work).

    Args:
        redis: source of the live counters.
        session_factory: opens per-row AsyncSessions. Pass
            ``async_sessionmaker`` already bound to the production
            engine; tests pass one bound to the test engine.

    Returns:
        Number of rows successfully UPSERTed. Malformed Redis keys
        and empty hashes are skipped silently (defensive — the
        record path never produces malformed keys, but a future
        manual ``HSET`` outside the recorder's control shouldn't
        crash the flush job).
    """
    flushed = 0
    async for raw_key in redis.scan_iter(match="tokens:*", count=_SCAN_BATCH):
        key = raw_key if isinstance(raw_key, str) else raw_key.decode()
        parsed = _parse_meter_key(key)
        if parsed is None:
            continue
        firm_id, model, day = parsed

        # redis-py's hgetall typing is `Awaitable[dict] | dict`; the
        # runtime is always awaitable in asyncio.Redis. Same narrow
        # ignore as the usage() method above.
        raw_fields = await redis.hgetall(key)  # type: ignore[misc]
        if not raw_fields:
            continue

        input_tokens = int(raw_fields.get("input_tokens", 0))
        output_tokens = int(raw_fields.get("output_tokens", 0))
        calls = int(raw_fields.get("calls", 0))

        async with session_factory() as session, firm_context(firm_id):
            stmt = pg_insert(TokenUsageRow).values(
                firm_id=firm_id,
                model=model,
                day=day,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                calls=calls,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["firm_id", "model", "day"],
                set_={
                    "input_tokens": stmt.excluded.input_tokens,
                    "output_tokens": stmt.excluded.output_tokens,
                    "calls": stmt.excluded.calls,
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.commit()

        flushed += 1
    return flushed


def _parse_meter_key(key: str) -> tuple[uuid.UUID, str, _dt.date] | None:
    """Parse ``tokens:{firm_uuid}:{model}:{day}`` into its components.

    Model strings may contain hyphens (``claude-sonnet-4-6``) but not
    colons — Anthropic model ids are colon-free in current and any
    plausible future naming. ``rsplit`` on the day side and a single
    split on the firm side makes the parser robust to model strings
    even if they grew unusual punctuation.
    """
    if not key.startswith("tokens:"):
        return None
    rest = key[len("tokens:") :]
    try:
        firm_str, after_firm = rest.split(":", 1)
        firm_id = uuid.UUID(firm_str)
    except (ValueError, AttributeError):
        return None
    try:
        model, day_str = after_firm.rsplit(":", 1)
        day = _dt.date.fromisoformat(day_str)
    except (ValueError, AttributeError):
        return None
    if not model:
        return None
    return firm_id, model, day
