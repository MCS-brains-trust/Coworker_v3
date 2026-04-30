import pytest
import uuid
from coworker.security.audit import append_audit, verify_chain
from coworker.db.models.audit import AuditLogEntry

@pytest.mark.asyncio
async def test_audit_append_and_verify(db_session):
    firm_id = str(uuid.uuid4())
    
    # Append first entry
    entry1 = await append_audit(
        db_session,
        firm_id=firm_id,
        actor_type="user",
        actor_id="user_1",
        action="login.success",
        payload={"ip": "127.0.0.1"}
    )
    
    # Append second entry
    entry2 = await append_audit(
        db_session,
        firm_id=firm_id,
        actor_type="user",
        actor_id="user_1",
        action="draft.created",
        target_type="draft",
        target_id="draft_1",
        payload={"title": "Test Draft"}
    )
    
    # Verify chain
    is_valid, broken_id = await verify_chain(db_session, firm_id)
    assert is_valid is True
    assert broken_id is None
    
    # Tamper with entry1
    entry1.payload = {"ip": "192.168.1.1"}
    db_session.add(entry1)
    await db_session.flush()
    
    # Verify chain again, should fail
    is_valid, broken_id = await verify_chain(db_session, firm_id)
    assert is_valid is False
    assert broken_id == entry1.id
