"""Microsoft Graph outbound rate limiting (in-memory).

Two layers of throttling sit between any caller in this codebase
and the Microsoft Graph API:

1. **Global token bucket** — 1000 requests per 60 seconds across the
   entire process. Microsoft documents Graph throttling at the
   "10,000 requests per 10 minutes per app" tier for most resources;
   1000/60 sits well inside that envelope with room for burst.

2. **Per-mailbox semaphore** — 4 concurrent in-flight requests per
   mailbox. Microsoft also applies per-mailbox throttling we cannot
   inspect; this caps the blast radius of a runaway plugin against
   any one user's mailbox.

Callers wrap each Graph call::

    async with rate_limiter.slot(mailbox_id="user-uuid"):
        response = await httpx_client.get(...)

If the bucket is empty the caller waits until a token refills. If
the semaphore is full the caller waits for another in-flight call
to release. **The semaphore is acquired first, the token second.**
A token represents one real outbound HTTP call, so we only consume
one when the call is about to actually go out — not while a caller
sits queued behind a busy mailbox.

In-memory and per-process. Phase 3I replaces this with a Redis-backed
sliding-window limiter for multi-worker correctness. After that, the
in-memory RateLimiter survives as the default in unit tests, where
asyncio scheduling makes the in-memory shape simpler to assert against.
"""
import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TokenBucket:
    """Async token bucket with continuous refill.

    Capacity is the maximum tokens that can accumulate. Refill is
    expressed as tokens-per-second; calls to `acquire` block when
    insufficient tokens are present and resume once the bucket has
    refilled enough.
    """

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self._capacity = float(capacity)
        self._refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        """Block until `n` tokens are available, then consume them."""
        if n <= 0:
            raise ValueError("n must be positive")
        if n > self._capacity:
            raise ValueError(
                f"n ({n}) exceeds bucket capacity ({self._capacity})"
            )
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait_seconds = deficit / self._refill_per_second
            # Sleep outside the lock — other callers can re-check meanwhile.
            await asyncio.sleep(wait_seconds)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._refill_per_second,
        )
        self._last_refill = now

    @property
    def tokens(self) -> float:
        """Current token count without applying refill (testing aid)."""
        return self._tokens


class MailboxSemaphores:
    """Lazy per-mailbox semaphore registry.

    `get(mailbox_id)` returns an ``asyncio.Semaphore`` configured at
    ``per_mailbox_limit`` concurrent holders. Semaphores are created on
    first use and pinned for the process lifetime; a mailbox that sees
    one request and then never again leaves a small idle semaphore
    object in memory, which is fine at the firm scales we target.
    """

    def __init__(self, per_mailbox_limit: int) -> None:
        if per_mailbox_limit <= 0:
            raise ValueError("per_mailbox_limit must be > 0")
        self._limit = per_mailbox_limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def get(self, mailbox_id: str) -> asyncio.Semaphore:
        async with self._lock:
            sem = self._semaphores.get(mailbox_id)
            if sem is None:
                sem = asyncio.Semaphore(self._limit)
                self._semaphores[mailbox_id] = sem
            return sem


class RateLimiter:
    """Combined per-process Graph rate limiter (bucket + per-mailbox)."""

    def __init__(
        self,
        *,
        global_capacity: int = 1000,
        global_window_seconds: float = 60.0,
        per_mailbox_limit: int = 4,
    ) -> None:
        self._bucket = TokenBucket(
            capacity=global_capacity,
            refill_per_second=global_capacity / global_window_seconds,
        )
        self._semaphores = MailboxSemaphores(per_mailbox_limit)

    @asynccontextmanager
    async def slot(self, mailbox_id: str) -> AsyncIterator[None]:
        """Acquire a per-mailbox slot and a global token, then yield.

        Order matters: per-mailbox semaphore first, global token
        second. A token (representing one real outbound HTTP call) is
        only consumed when the call is about to actually go out, not
        while waiting behind a busy mailbox.
        """
        sem = await self._semaphores.get(mailbox_id)
        async with sem:
            await self._bucket.acquire()
            yield


_DEFAULT_RATE_LIMITER = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide default rate limiter.

    Tests construct their own `RateLimiter` instances with tighter
    parameters; production code routes through this default.
    """
    return _DEFAULT_RATE_LIMITER
