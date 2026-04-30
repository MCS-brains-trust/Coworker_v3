import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.config import get_settings
from coworker.db.models.audit import AuditLogEntry


async def append_audit(
    session: AsyncSession,
    *,
    firm_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Append a hash-chained entry to the audit log.

    Must be called within an existing session/transaction. The chain integrity
    relies on this being called serially per firm; the caller is responsible
    for serialisation (we use a Postgres advisory lock per firm_id).
    """
    settings = get_settings()
    payload = payload or {}

    # Acquire advisory lock keyed on firm_id (prevents concurrent appends)
    firm_lock_key = int(hashlib.sha256(firm_id.encode()).hexdigest()[:8], 16)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": firm_lock_key}
    )

    # Get prev hash
    last = await session.execute(
        select(AuditLogEntry.entry_hash)
        .where(AuditLogEntry.firm_id == firm_id)
        .order_by(AuditLogEntry.id.desc())
        .limit(1)
    )
    prev_hash = last.scalar_one_or_none() or settings.AUDIT_LOG_GENESIS_HASH

    canonical = json.dumps(
        {
            "firm_id": firm_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    entry_hash = hashlib.sha256(canonical.encode()).hexdigest()

    entry = AuditLogEntry(
        firm_id=firm_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession, firm_id: str) -> tuple[bool, int | None]:
    """Walk the entire audit log chain for a firm and verify each hash.

    Returns (True, None) on success, (False, broken_id) on tamper detection.
    """
    settings = get_settings()
    result = await session.stream(
        select(AuditLogEntry)
        .where(AuditLogEntry.firm_id == firm_id)
        .order_by(AuditLogEntry.id.asc())
    )
    expected_prev = settings.AUDIT_LOG_GENESIS_HASH
    async for (entry,) in result:
        if entry.prev_hash != expected_prev:
            return False, entry.id
        canonical = json.dumps(
            {
                "firm_id": str(entry.firm_id),
                "actor_type": entry.actor_type,
                "actor_id": entry.actor_id,
                "action": entry.action,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                "payload": entry.payload,
                "prev_hash": entry.prev_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(canonical.encode()).hexdigest() != entry.entry_hash:
            return False, entry.id
        expected_prev = entry.entry_hash
    return True, None
