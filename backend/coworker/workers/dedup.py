"""Per-(firm, plugin, event) dedup for plugin runs.

Microsoft doesn't guarantee exactly-once delivery for change
notifications: the same message_id can arrive twice via the
webhook, and the Phase 11-7 backfill re-enqueues messages it
finds in the catch-up window — some of which the regular
webhook also delivered. Without dedup, smart_responder would
draft the same reply twice; correspondence_logger would write
two client_interaction rows.

The dedup boundary lives at the per-plugin per-event level
rather than at the queue level. Different plugins running on
the same email is the intended fan-out behaviour; the same
plugin running twice for the same email isn't.

Implementation: a Redis SET (well — a SET-NX'd KEY with TTL)
per ``(firm_id, plugin_name, dedup_key)``. The dedup_key is
derived from the event:

    email_received  -> ``email:{message_id}``
    calendar_event  -> ``calendar:{event_id}``
    scheduled       -> ``scheduled:{scheduled_at}``
    other triggers  -> None  (no dedup applied)

TTL defaults to 24 hours — well past the Phase 11-7 backfill
window (5 minutes), and well before the next legitimate
re-firing of any plugin against the same resource.

Why Redis + key-with-TTL rather than a DB table:
- Concurrency: SET NX is atomic; two workers pulling the same
  event in the same window will deterministically pick one
  winner.
- Reset cost: TTL handles cleanup automatically.
- No new migration; no FK lifecycle concerns.
"""
import datetime as _dt

from loguru import logger
from redis.asyncio import Redis

from coworker.workers.plugin_queue import PluginEvent

# How long a dedup claim survives. Long enough to outlast any
# reasonable backfill / redelivery window; short enough that an
# operator who wants to re-run a plugin against the same event
# can wait it out (or DELETE the key by hand) instead of waiting
# weeks.
DEFAULT_TTL = _dt.timedelta(hours=24)


def derive_dedup_key(event: PluginEvent) -> str | None:
    """Return the per-event dedup token, or None if dedup doesn't apply.

    For an ``email_received`` event the natural key is
    ``message_id``; for ``calendar_event`` it's the event id
    (Microsoft uses the same ``resourceData.id`` field for both);
    for ``scheduled`` it's the scheduled-tick timestamp.
    """
    data = event.event_data
    if event.trigger == "email_received":
        mid = data.get("message_id")
        if isinstance(mid, str) and mid:
            return f"email:{mid}"
        return None
    if event.trigger == "calendar_event":
        eid = data.get("message_id")  # same notification shape
        if isinstance(eid, str) and eid:
            return f"calendar:{eid}"
        return None
    if event.trigger == "scheduled":
        sched = data.get("scheduled_at")
        if isinstance(sched, str) and sched:
            return f"scheduled:{sched}"
        return None
    return None


class PluginRunDedup:
    """Redis-backed dedup gate.

    Usage from the processor::

        dedup = PluginRunDedup(redis)
        if not await dedup.claim(firm_id, plugin_name, event):
            return "deduped"

    Construction is cheap; share one instance across the BRPOP
    loop's workers. The instance holds a reference to the Redis
    client and nothing else.
    """

    _KEY_PREFIX = "plugin_dedup"

    def __init__(
        self, redis: Redis, *, ttl: _dt.timedelta = DEFAULT_TTL,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = int(ttl.total_seconds())

    async def claim(
        self,
        firm_id: object,
        plugin_name: str,
        event: PluginEvent,
    ) -> bool:
        """Reserve the run.

        Returns True iff this is the first claim for the
        ``(firm_id, plugin_name, dedup_key)`` triple within the
        TTL window. Returns False if another caller already
        claimed it (deduped), or if the event has no derivable
        dedup key (treat as "no dedup applies, let it run" —
        the caller MUST return True in that case, see below).
        """
        token = derive_dedup_key(event)
        if token is None:
            # Triggers without a natural key (fusesign_event,
            # manual, …) get no dedup. Caller proceeds.
            return True
        key = f"{self._KEY_PREFIX}:{firm_id}:{plugin_name}:{token}"
        # NX = only set if not exists. EX in seconds. redis-py's
        # async client returns True / None depending on whether
        # the key was set.
        acquired = await self._redis.set(
            key, "1", nx=True, ex=self._ttl_seconds,
        )
        if not acquired:
            logger.info(
                "plugin dedup hit firm_id={} plugin={} key={}",
                firm_id, plugin_name, token,
            )
            return False
        return True
