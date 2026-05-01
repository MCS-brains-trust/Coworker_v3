import uuid

import pytest

from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit, verify_chain


@pytest.mark.asyncio
async def test_audit_append_and_verify(db_session):
    # audit_log.firm_id has a FK to firms.id; insert a Firm row first so
    # append_audit doesn't violate the constraint. All NOT NULL columns
    # without a server default are set explicitly so the test does not
    # depend on the model's Python-level defaults firing at flush time.
    firm = Firm(
        name="Test Firm",
        slug=f"test-firm-{uuid.uuid4().hex[:8]}",
        timezone="Australia/Melbourne",
        shadow_mode=True,
        is_active=True,
        sharepoint_clients_folder_path="/Server/Clients",
        settings={},
    )
    db_session.add(firm)
    await db_session.flush()
    firm_id = str(firm.id)

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
