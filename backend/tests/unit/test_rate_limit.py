"""Tests for `coworker.graph.rate_limit`.

These tests deliberately use small capacities and short windows to
keep wall-clock time bounded. They assert relative timing within
generous bands; CI scheduling jitter on slow runners would push a
tight assertion into flakiness.
"""
import asyncio
import time

import pytest
from coworker.graph.rate_limit import (
    MailboxSemaphores,
    RateLimiter,
    TokenBucket,
)


class TestTokenBucket:
    async def test_acquire_consumes_tokens(self) -> None:
        bucket = TokenBucket(capacity=10, refill_per_second=0.1)
        await bucket.acquire(5)
        # ~0 elapsed; refill negligible; 10 - 5 ≈ 5
        assert 4.9 < bucket.tokens < 5.5

    async def test_acquire_returns_immediately_when_full(self) -> None:
        bucket = TokenBucket(capacity=10, refill_per_second=1)
        start = time.monotonic()
        await bucket.acquire(5)
        assert time.monotonic() - start < 0.05

    async def test_acquire_waits_when_exhausted(self) -> None:
        # 2 tokens, refilling at 2/sec. Drain then wait for one more.
        bucket = TokenBucket(capacity=2, refill_per_second=2)
        await bucket.acquire(2)
        start = time.monotonic()
        await bucket.acquire(1)
        elapsed = time.monotonic() - start
        # Need 1 token at 2/sec ≈ 0.5s. Allow a generous band for
        # asyncio scheduling jitter.
        assert 0.3 < elapsed < 0.9

    async def test_concurrent_acquires_serialise_through_refill(self) -> None:
        # 2 capacity, 10/sec refill. Five concurrent acquire(1):
        # first two fast, remaining three wait for ~0.3s of refill.
        bucket = TokenBucket(capacity=2, refill_per_second=10)
        start = time.monotonic()
        await asyncio.gather(*[bucket.acquire(1) for _ in range(5)])
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25

    async def test_n_must_be_positive(self) -> None:
        bucket = TokenBucket(capacity=10, refill_per_second=1)
        with pytest.raises(ValueError):
            await bucket.acquire(0)
        with pytest.raises(ValueError):
            await bucket.acquire(-1)

    async def test_n_must_not_exceed_capacity(self) -> None:
        bucket = TokenBucket(capacity=10, refill_per_second=1)
        with pytest.raises(ValueError):
            await bucket.acquire(11)

    def test_construction_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_per_second=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=-1, refill_per_second=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=10, refill_per_second=0)
        with pytest.raises(ValueError):
            TokenBucket(capacity=10, refill_per_second=-1)


class TestMailboxSemaphores:
    async def test_same_mailbox_returns_same_semaphore(self) -> None:
        sems = MailboxSemaphores(per_mailbox_limit=4)
        a1 = await sems.get("alice")
        a2 = await sems.get("alice")
        assert a1 is a2

    async def test_different_mailboxes_get_separate_semaphores(self) -> None:
        sems = MailboxSemaphores(per_mailbox_limit=4)
        alice = await sems.get("alice")
        bob = await sems.get("bob")
        assert alice is not bob

    async def test_semaphore_caps_concurrent_holders(self) -> None:
        sems = MailboxSemaphores(per_mailbox_limit=2)
        sem = await sems.get("alice")
        in_flight = 0
        max_in_flight = 0

        async def worker() -> None:
            nonlocal in_flight, max_in_flight
            async with sem:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1

        await asyncio.gather(*[worker() for _ in range(10)])
        assert max_in_flight == 2

    def test_invalid_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            MailboxSemaphores(per_mailbox_limit=0)
        with pytest.raises(ValueError):
            MailboxSemaphores(per_mailbox_limit=-1)


class TestRateLimiter:
    async def test_slot_acquires_and_releases_both_layers(self) -> None:
        limiter = RateLimiter(
            global_capacity=10,
            global_window_seconds=1,
            per_mailbox_limit=2,
        )
        async with limiter.slot("alice"):
            pass

    async def test_slot_serialises_per_mailbox(self) -> None:
        limiter = RateLimiter(
            global_capacity=100,
            global_window_seconds=1,
            per_mailbox_limit=2,
        )
        in_flight = 0
        max_in_flight = 0

        async def worker() -> None:
            nonlocal in_flight, max_in_flight
            async with limiter.slot("alice"):
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1

        await asyncio.gather(*[worker() for _ in range(5)])
        assert max_in_flight == 2

    async def test_different_mailboxes_run_in_parallel(self) -> None:
        # Even with per_mailbox_limit=1, two distinct mailboxes can
        # be in-flight concurrently because each gets its own slot.
        limiter = RateLimiter(
            global_capacity=100,
            global_window_seconds=1,
            per_mailbox_limit=1,
        )
        in_flight = 0
        max_in_flight = 0

        async def worker(mailbox: str) -> None:
            nonlocal in_flight, max_in_flight
            async with limiter.slot(mailbox):
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(worker("alice"), worker("bob"))
        assert max_in_flight == 2

    async def test_global_token_exhaustion_blocks_across_mailboxes(self) -> None:
        # Tiny global capacity (2), large per-mailbox (10). Five workers
        # across different mailboxes: first two run fast, last three wait
        # on the global bucket regardless of mailbox.
        limiter = RateLimiter(
            global_capacity=2,
            global_window_seconds=1,
            per_mailbox_limit=10,
        )
        start = time.monotonic()
        await asyncio.gather(
            *[
                _enter_and_exit(limiter, f"mbx-{i}")
                for i in range(5)
            ]
        )
        elapsed = time.monotonic() - start
        # Need 3 more tokens at 2/sec ⇒ at least 1.5s of global waiting.
        assert elapsed >= 1.0


async def _enter_and_exit(limiter: RateLimiter, mailbox: str) -> None:
    async with limiter.slot(mailbox):
        return None
