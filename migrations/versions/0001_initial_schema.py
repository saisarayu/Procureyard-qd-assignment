"""Initial schema migration for QuickDrop payout desk

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-10-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from app.models.database import GUID

# revision identifiers, used by Alembic.
revision: str = '0001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # riders table
    op.create_table(
        'riders',
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('city', sa.String(length=50), nullable=False),
        sa.Column('joined_on', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('rider_id')
    )

    # trips table
    op.create_table(
        'trips',
        sa.Column('trip_id', sa.String(length=20), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('distance_km', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('surge_multiplier', sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('trip_id')
    )
    op.create_index('ix_trips_rider_date', 'trips', ['rider_id', 'started_at'], unique=False)
    op.create_index(op.f('ix_trips_rider_id'), 'trips', ['rider_id'], unique=False)

    # payout_lines table
    op.create_table(
        'payout_lines',
        sa.Column('line_id', sa.String(length=20), nullable=False),
        sa.Column('payout_date', sa.Date(), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('line_type', sa.String(length=30), nullable=False),
        sa.Column('trip_id', sa.String(length=20), nullable=True),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('line_id')
    )
    op.create_index('ix_payout_rider_date', 'payout_lines', ['rider_id', 'payout_date'], unique=False)
    op.create_index(op.f('ix_payout_lines_payout_date'), 'payout_lines', ['payout_date'], unique=False)
    op.create_index(op.f('ix_payout_lines_rider_id'), 'payout_lines', ['rider_id'], unique=False)
    op.create_index(op.f('ix_payout_lines_trip_id'), 'payout_lines', ['trip_id'], unique=False)

    # conversations table
    op.create_table(
        'conversations',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('conversation_id', sa.String(length=100), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['rider_id'], ['riders.rider_id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversations_conversation_id'), 'conversations', ['conversation_id'], unique=True)
    op.create_index(op.f('ix_conversations_rider_id'), 'conversations', ['rider_id'], unique=False)

    # messages table
    op.create_table(
        'messages',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('message_id', sa.String(length=100), nullable=False),
        sa.Column('conversation_id', GUID(), nullable=True),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reply', sa.Text(), nullable=True),
        sa.Column('processed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
        sa.ForeignKeyConstraint(['rider_id'], ['riders.rider_id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messages_conversation_id'), 'messages', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_messages_message_id'), 'messages', ['message_id'], unique=True)
    op.create_index(op.f('ix_messages_rider_id'), 'messages', ['rider_id'], unique=False)

    # disputes table
    op.create_table(
        'disputes',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('dispute_id', sa.String(length=50), nullable=True),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('message_id', GUID(), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('trip_id', sa.String(length=20), nullable=True),
        sa.Column('disputed_date', sa.Date(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('expected_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('actual_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('shortfall', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
        sa.ForeignKeyConstraint(['rider_id'], ['riders.rider_id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_disputes_dispute_id'), 'disputes', ['dispute_id'], unique=True)
    op.create_index(op.f('ix_disputes_rider_id'), 'disputes', ['rider_id'], unique=False)
    op.create_index(op.f('ix_disputes_trip_id'), 'disputes', ['trip_id'], unique=False)

    # dispute_evidence table
    op.create_table(
        'dispute_evidence',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('dispute_id', GUID(), nullable=False),
        sa.Column('evidence_type', sa.String(length=50), nullable=False),
        sa.Column('data', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['dispute_id'], ['disputes.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dispute_evidence_dispute_id'), 'dispute_evidence', ['dispute_id'], unique=False)

    # payment_attempts table
    op.create_table(
        'payment_attempts',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('dispute_id', GUID(), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('idempotency_key', sa.String(length=100), nullable=False),
        sa.Column('payswift_payout_id', sa.String(length=100), nullable=True),
        sa.Column('payswift_status', sa.String(length=30), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('reference', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['dispute_id'], ['disputes.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('ix_payment_rider_date', 'payment_attempts', ['rider_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_payment_attempts_dispute_id'), 'payment_attempts', ['dispute_id'], unique=False)
    op.create_index(op.f('ix_payment_attempts_rider_id'), 'payment_attempts', ['rider_id'], unique=False)

    # approval_requests table
    op.create_table(
        'approval_requests',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('dispute_id', GUID(), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('evidence_summary', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reviewer', sa.String(length=100), nullable=True),
        sa.Column('decision_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['dispute_id'], ['disputes.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dispute_id')
    )
    op.create_index(op.f('ix_approval_requests_rider_id'), 'approval_requests', ['rider_id'], unique=False)

    # audit_events table
    op.create_table(
        'audit_events',
        sa.Column('id', GUID(), nullable=False),
        sa.Column('rider_id', sa.String(length=10), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('input_data', sa.Text(), nullable=True),
        sa.Column('output_data', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_rider_time', 'audit_events', ['rider_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_rider_id'), 'audit_events', ['rider_id'], unique=False)


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('approval_requests')
    op.drop_table('payment_attempts')
    op.drop_table('dispute_evidence')
    op.drop_table('disputes')
    op.drop_table('messages')
    op.drop_table('conversations')
    op.drop_table('payout_lines')
    op.drop_table('trips')
    op.drop_table('riders')
