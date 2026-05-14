"""Integration tests for the approval queue helpers.

Real DB. RLS enforcement is the security backstop, so we exercise
both the happy paths and the cross-firm isolation.
"""
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.approval.items import (
    ApprovalTransitionError,
    CreateApprovalInput,
    approve,
    create_approval,
    edit_payload,
    get_by_id,
    list_pending,
    reject,
)
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def approval_env(test_database_url) -> AsyncIterator[dict]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log", "approval_items")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("approval_items", "audit_log", "users"):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
                    {"id": str(firm_id)},
                )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm_user(sm) -> tuple[uuid.UUID, uuid.UUID]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id, name="Approval Firm",
            slug=f"a-{uuid.uuid4().hex[:8]}",
        )
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Principal",
        )
        session.add_all([firm, user])
        await session.commit()
        return firm_id, user.id


def _draft(*, summary: str = "Draft 1") -> CreateApprovalInput:
    return CreateApprovalInput(
        plugin_name="smart_responder",
        category="email_draft",
        summary=summary,
        payload={
            "to": ["client@example.com"],
            "subject": "Re: your query",
            "body_html": "<p>Hi,</p>",
        },
    )


# ===========================================================================
# Tests
# ===========================================================================


async def test_create_approval_persists_pending_row(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, _ = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id, input=_draft(summary="Hello"),
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        persisted = (
            await session.execute(
                select(ApprovalItem).where(ApprovalItem.id == row.id)
            )
        ).scalar_one()
        assert persisted.status == "pending"
        assert persisted.summary == "Hello"
        assert persisted.payload["subject"] == "Re: your query"
        assert persisted.plugin_name == "smart_responder"
        assert persisted.decided_at is None


async def test_list_pending_returns_only_pending(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        a = await create_approval(session, firm_id, input=_draft(summary="A"))
        b = await create_approval(session, firm_id, input=_draft(summary="B"))
        c = await create_approval(session, firm_id, input=_draft(summary="C"))
        await session.commit()

    # Approve A, reject B; only C remains pending.
    async with sm() as session, firm_context(firm_id):
        await approve(session, a.id, decided_by_user_id=user_id)
        await reject(session, b.id, decided_by_user_id=user_id)
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        pending = await list_pending(session, firm_id)
        assert [p.id for p in pending] == [c.id]


async def test_approve_transitions_to_approved(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        decided = await approve(
            session, row.id,
            decided_by_user_id=user_id,
            notes="LGTM",
        )
        await session.commit()

    assert decided.status == "approved"
    assert decided.decided_at is not None
    assert decided.decided_by_user_id == user_id
    assert decided.decision_notes == "LGTM"


async def test_reject_transitions_to_rejected(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        decided = await reject(
            session, row.id,
            decided_by_user_id=user_id,
            notes="Wrong tone",
        )
        await session.commit()

    assert decided.status == "rejected"
    assert decided.decision_notes == "Wrong tone"


async def test_cannot_approve_already_approved(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user_id)
        await session.commit()

        with pytest.raises(ApprovalTransitionError, match="approved"):
            await approve(session, row.id, decided_by_user_id=user_id)


async def test_cannot_reject_already_rejected(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        await reject(session, row.id, decided_by_user_id=user_id)
        await session.commit()

        with pytest.raises(ApprovalTransitionError, match="rejected"):
            await reject(session, row.id, decided_by_user_id=user_id)


async def test_lookup_missing_id_raises(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        with pytest.raises(LookupError):
            await approve(
                session, uuid.uuid4(), decided_by_user_id=user_id,
            )


async def test_rls_blocks_cross_firm_read(approval_env) -> None:
    """An item created by firm A is invisible to a session scoped to firm B."""
    sm = approval_env["sm"]
    firm_a_id, _ = await _seed_firm_user(sm)
    approval_env["created"].append(firm_a_id)
    firm_b_id, _ = await _seed_firm_user(sm)
    approval_env["created"].append(firm_b_id)

    async with sm() as session, firm_context(firm_a_id):
        row_a = await create_approval(session, firm_a_id, input=_draft())
        await session.commit()

    async with sm() as session, firm_context(firm_b_id):
        # firm B cannot see firm A's row.
        assert await get_by_id(session, row_a.id) is None
        # ... and its own pending list is empty.
        assert await list_pending(session, firm_b_id) == []


async def test_check_constraint_rejects_unknown_status(approval_env) -> None:
    """The DB-side CHECK constraint catches invalid status values that
    bypass the helper functions (e.g. a direct UPDATE in a migration)."""
    from sqlalchemy.exc import IntegrityError

    sm = approval_env["sm"]
    firm_id, _ = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        # Bypass the helper: write a status the migration rejects.
        row.status = "totally_invented"
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# ===========================================================================
# Phase 9-3: in-place edit
# ===========================================================================


async def test_edit_payload_replaces_payload(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()

        edited = await edit_payload(
            session, row.id,
            new_payload={
                "to": ["client@example.com"],
                "subject": "Re: your query (edited)",
                "body_html": "<p>Better wording.</p>",
            },
            edited_by_user_id=user_id,
        )
        await session.commit()

    assert edited.status == "pending"
    assert edited.payload["subject"] == "Re: your query (edited)"
    assert edited.payload["body_html"] == "<p>Better wording.</p>"
    assert edited.last_edited_at is not None
    assert edited.last_edited_by_user_id == user_id


async def test_edit_payload_after_approve_raises(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user_id)
        await session.commit()

        with pytest.raises(ApprovalTransitionError, match="pending"):
            await edit_payload(
                session, row.id,
                new_payload={"anything": "else"},
                edited_by_user_id=user_id,
            )


async def test_edit_payload_missing_id_raises(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        with pytest.raises(LookupError):
            await edit_payload(
                session, uuid.uuid4(),
                new_payload={"x": 1},
                edited_by_user_id=user_id,
            )


# ===========================================================================
# Phase 9-6: two-person approval
# ===========================================================================


def _two_person_input(*, summary: str = "Engagement letter") -> CreateApprovalInput:
    """An input whose category is in TWO_PERSON_REQUIRED_CATEGORIES."""
    return CreateApprovalInput(
        plugin_name="engagement_letter",
        category="engagement_letter",
        summary=summary,
        payload={"client_name": "Acme Pty Ltd"},
    )


async def _seed_second_user(sm, firm_id) -> uuid.UUID:
    """A second user in the same firm — for the cosigner."""
    from coworker.db.models import User
    async with sm() as session, firm_context(firm_id):
        u = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u2-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Cosigner",
        )
        session.add(u)
        await session.commit()
        return u.id


async def test_two_person_category_requires_two_signatures(
    approval_env,
) -> None:
    sm = approval_env["sm"]
    firm_id, user_a = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)
    user_b = await _seed_second_user(sm, firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id, input=_two_person_input(),
        )
        await session.commit()

    assert row.required_approvals == 2

    # First approver — signature recorded but still pending.
    async with sm() as session, firm_context(firm_id):
        after_first = await approve(
            session, row.id, decided_by_user_id=user_a, notes="LGTM",
        )
        await session.commit()

    assert after_first.status == "pending"
    assert len(after_first.approval_signatures) == 1
    assert after_first.approval_signatures[0]["user_id"] == str(user_a)

    # Second approver (different user) — transitions to approved.
    async with sm() as session, firm_context(firm_id):
        after_second = await approve(
            session, row.id, decided_by_user_id=user_b, notes="LGTM 2",
        )
        await session.commit()

    assert after_second.status == "approved"
    assert len(after_second.approval_signatures) == 2
    assert after_second.decided_by_user_id == user_b


async def test_two_person_same_user_cannot_sign_twice(approval_env) -> None:
    sm = approval_env["sm"]
    firm_id, user_a = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id, input=_two_person_input(),
        )
        await session.commit()
        await approve(session, row.id, decided_by_user_id=user_a)
        await session.commit()

        with pytest.raises(ApprovalTransitionError, match="already signed"):
            await approve(session, row.id, decided_by_user_id=user_a)


async def test_single_person_category_records_signature(approval_env) -> None:
    """Even the default single-person path keeps an audit signature."""
    sm = approval_env["sm"]
    firm_id, user_id = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(session, firm_id, input=_draft())
        await session.commit()
        decided = await approve(
            session, row.id, decided_by_user_id=user_id, notes="LGTM",
        )
        await session.commit()

    assert decided.required_approvals == 1
    assert decided.status == "approved"
    assert len(decided.approval_signatures) == 1
    assert decided.approval_signatures[0]["user_id"] == str(user_id)
    assert decided.approval_signatures[0]["notes"] == "LGTM"


async def test_two_person_reject_is_terminal_on_first_call(
    approval_env,
) -> None:
    """Any one reviewer can veto — no second signature needed for reject."""
    sm = approval_env["sm"]
    firm_id, user_a = await _seed_firm_user(sm)
    approval_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id, input=_two_person_input(),
        )
        await session.commit()
        rejected = await reject(
            session, row.id, decided_by_user_id=user_a, notes="No.",
        )
        await session.commit()

    assert rejected.status == "rejected"
