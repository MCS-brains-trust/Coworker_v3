"""Phase 2: Identity, Tenancy, and Security Foundations

Revision ID: 65f1a5282b54
Revises: 
Create Date: 2026-04-30 01:58:17.629683

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65f1a5282b54'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create firms table
    op.create_table(
        'firms',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('slug', sa.String(length=100), nullable=False),
        sa.Column('abn', sa.String(length=11), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('timezone', sa.String(length=50), nullable=False),
        sa.Column('shadow_mode', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('azure_tenant_id', sa.String(length=100), nullable=True),
        sa.Column('azure_client_id', sa.String(length=100), nullable=True),
        sa.Column('azure_client_secret_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('anthropic_api_key_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('xpm_account_id', sa.String(length=100), nullable=True),
        sa.Column('xpm_client_id', sa.String(length=100), nullable=True),
        sa.Column('xpm_client_secret_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('xpm_access_token_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('xpm_refresh_token_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('xpm_token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fusesign_api_key_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('teams_webhook_url_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('sharepoint_site_id', sa.String(length=200), nullable=True),
        sa.Column('sharepoint_clients_drive_id', sa.String(length=200), nullable=True),
        sa.Column('sharepoint_clients_folder_path', sa.String(length=500), nullable=False),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug')
    )

    # Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('firm_id', sa.UUID(), nullable=False),
        sa.Column('azure_object_id', sa.String(length=100), nullable=False),
        sa.Column('upn', sa.String(length=200), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('mail', sa.String(length=200), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('ms_access_token_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('ms_refresh_token_ciphertext', sa.LargeBinary(), nullable=True),
        sa.Column('ms_token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('monitored_mailbox', sa.String(length=200), nullable=True),
        sa.Column('is_active_processor', sa.Boolean(), nullable=False),
        sa.Column('is_reception_mode', sa.Boolean(), nullable=False),
        sa.Column('style_profile', sa.JSON(), nullable=True),
        sa.Column('style_profile_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_azure_object_id'), 'users', ['azure_object_id'], unique=True)
    op.create_index(op.f('ix_users_firm_id_role'), 'users', ['firm_id', 'role'], unique=False)
    op.create_index(op.f('ix_users_mail'), 'users', ['mail'], unique=False)
    op.create_index(op.f('ix_users_upn'), 'users', ['upn'], unique=True)

    # Create audit_log table
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('firm_id', sa.UUID(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor_type', sa.String(length=20), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=True),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('target_type', sa.String(length=50), nullable=True),
        sa.Column('target_id', sa.String(length=200), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('prev_hash', sa.String(length=64), nullable=False),
        sa.Column('entry_hash', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entry_hash')
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_audit_log_firm_id'), 'audit_log', ['firm_id'], unique=False)
    op.create_index('ix_audit_firm_action_time', 'audit_log', ['firm_id', 'action', 'occurred_at'], unique=False)
    op.create_index(op.f('ix_audit_log_occurred_at'), 'audit_log', ['occurred_at'], unique=False)

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_audit_log_occurred_at'), table_name='audit_log')
    op.drop_index('ix_audit_firm_action_time', table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_firm_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_users_upn'), table_name='users')
    op.drop_index(op.f('ix_users_mail'), table_name='users')
    op.drop_index(op.f('ix_users_firm_id_role'), table_name='users')
    op.drop_index(op.f('ix_users_azure_object_id'), table_name='users')
    op.drop_table('users')
    op.drop_table('firms')
