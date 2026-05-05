"""Integration tests for the bootstrap-firm CLI command.

Exercises the inner async helper (`_bootstrap_firm`) directly against the
test database. The helper takes an injected AsyncSession; the click
wrapper is just an asyncio.run + SessionLocal shim around it.
"""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.cli.main import _bootstrap_firm
from coworker.db.models.tenancy import Firm
from coworker.db.session import firm_context
from coworker.security.encryption import decrypt_str


async def _set_firm_id(session: AsyncSession, firm_id: uuid.UUID) -> None:
    """Apply the app.firm_id GUC mid-transaction so a subsequent SELECT
    under FORCE RLS can see the row. The Session after_begin listener
    only fires at transaction start; the test fixture wraps everything
    in a single outer transaction so we set the GUC explicitly.
    """
    await session.execute(
        text("SELECT set_config('app.firm_id', :v, true)"),
        {"v": str(firm_id)},
    )


@pytest.mark.asyncio
async def test_bootstrap_firm_creates_new(db_session: AsyncSession) -> None:
    tenant_id = str(uuid.uuid4())
    client_id = str(uuid.uuid4())

    firm_id = await _bootstrap_firm(
        db_session,
        slug="acme-co",
        name="Acme Co",
        azure_tenant_id=tenant_id,
        azure_client_id=client_id,
        azure_client_secret="initial-secret",
        timezone="Australia/Sydney",
        abn="12345678901",
    )

    async with firm_context(firm_id):
        await _set_firm_id(db_session, firm_id)
        firm = (
            await db_session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        assert firm.slug == "acme-co"
        assert firm.name == "Acme Co"
        assert firm.azure_tenant_id == tenant_id
        assert firm.azure_client_id == client_id
        assert firm.timezone == "Australia/Sydney"
        assert firm.abn == "12345678901"

        plaintext = decrypt_str(
            firm.azure_client_secret_ciphertext, firm_id=str(firm_id)
        )
        assert plaintext == "initial-secret"


@pytest.mark.asyncio
async def test_bootstrap_firm_is_idempotent_on_slug(db_session: AsyncSession) -> None:
    tenant_id = str(uuid.uuid4())
    client_id_v1 = str(uuid.uuid4())
    client_id_v2 = str(uuid.uuid4())

    first_id = await _bootstrap_firm(
        db_session,
        slug="rotating-co",
        name="Rotating Co",
        azure_tenant_id=tenant_id,
        azure_client_id=client_id_v1,
        azure_client_secret="secret-v1",
    )

    second_id = await _bootstrap_firm(
        db_session,
        slug="rotating-co",
        name="IGNORED — bootstrap does not change name on update",
        azure_tenant_id=tenant_id,
        azure_client_id=client_id_v2,
        azure_client_secret="secret-v2",
    )

    assert first_id == second_id, "re-running with same slug must reuse the row"

    async with firm_context(first_id):
        await _set_firm_id(db_session, first_id)
        firm = (
            await db_session.execute(select(Firm).where(Firm.id == first_id))
        ).scalar_one()
        assert firm.name == "Rotating Co", "name is preserved across re-bootstrap"
        assert firm.azure_client_id == client_id_v2, "client_id is rotated"

        plaintext = decrypt_str(
            firm.azure_client_secret_ciphertext, firm_id=str(first_id)
        )
        assert plaintext == "secret-v2", "secret is rotated"


@pytest.mark.asyncio
async def test_bootstrap_firm_secret_decrypts_with_firm_id_aad(
    db_session: AsyncSession,
) -> None:
    """The stored ciphertext is bound to firm_id via envelope AAD —
    decrypting with a different firm_id must fail (cross-firm boundary).
    """
    from cryptography.exceptions import InvalidTag

    firm_id = await _bootstrap_firm(
        db_session,
        slug="aad-test-co",
        name="AAD Test Co",
        azure_tenant_id=str(uuid.uuid4()),
        azure_client_id=str(uuid.uuid4()),
        azure_client_secret="bound-to-firm",
    )

    async with firm_context(firm_id):
        await _set_firm_id(db_session, firm_id)
        firm = (
            await db_session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()

        assert (
            decrypt_str(firm.azure_client_secret_ciphertext, firm_id=str(firm_id))
            == "bound-to-firm"
        )

        wrong_firm = str(uuid.uuid4())
        with pytest.raises(InvalidTag):
            decrypt_str(firm.azure_client_secret_ciphertext, firm_id=wrong_firm)
