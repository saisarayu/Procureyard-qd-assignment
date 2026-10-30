"""SQLAlchemy ORM models for the QuickDrop dispute system."""
import uuid
import enum
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Date, Text, Boolean,
    ForeignKey, UniqueConstraint, Index, Enum as SAEnum, Numeric
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.database.connection import Base


class GUID(TypeDecorator):
    """Platform-independent GUID type.
    Uses PostgreSQL's UUID type, otherwise uses CHAR(36).
    """
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid.UUID):
            try:
                return str(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                return str(value)
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid.UUID):
            try:
                return uuid.UUID(str(value))
            except (ValueError, TypeError):
                return value
        return value


class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AUTO_PAID = "auto_paid"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    ESCALATED = "escalated"
    CLOSED = "closed"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    VERIFICATION_REQUIRED = "verification_required"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class OpsItemType(str, enum.Enum):
    APPROVAL = "approval"
    ESCALATION = "escalation"


def utc_now():
    return datetime.now(timezone.utc)


# ── Static data loaded from CSV ──────────────────────────────────────

class Rider(Base):
    __tablename__ = "riders"

    rider_id = Column(String(10), primary_key=True)
    name = Column(String(100), nullable=False)
    city = Column(String(50), nullable=False)
    joined_on = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    messages = relationship("Message", back_populates="rider")
    disputes = relationship("Dispute", back_populates="rider")
    conversations = relationship("Conversation", back_populates="rider")


class Trip(Base):
    __tablename__ = "trips"

    trip_id = Column(String(20), primary_key=True)
    rider_id = Column(String(10), index=True, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(30), nullable=False)
    distance_km = Column(Numeric(10, 2), nullable=False)
    surge_multiplier = Column(Numeric(4, 2), nullable=False, default=Decimal("1.00"))
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_trips_rider_date", "rider_id", "started_at"),
    )


class PayoutLine(Base):
    __tablename__ = "payout_lines"

    line_id = Column(String(20), primary_key=True)
    payout_date = Column(Date, nullable=False, index=True)
    rider_id = Column(String(10), index=True, nullable=False)
    line_type = Column(String(30), nullable=False)  # trip, daily_incentive, cancellation_penalty
    trip_id = Column(String(20), nullable=True, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_payout_rider_date", "rider_id", "payout_date"),
    )


# ── Conversation & Dispute tracking ─────────────────────────────────

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(String(100), unique=True, nullable=False, index=True)
    rider_id = Column(String(10), ForeignKey("riders.rider_id"), nullable=False, index=True)
    status = Column(String(30), default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    rider = relationship("Rider", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    message_id = Column(String(100), unique=True, nullable=False, index=True)  # wamid for idempotency
    conversation_id = Column(GUID(), ForeignKey("conversations.id"), nullable=True, index=True)
    rider_id = Column(String(10), ForeignKey("riders.rider_id"), nullable=False, index=True)
    role = Column(String(20), default="rider", nullable=False)  # rider, assistant, system
    text = Column(Text, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    reply = Column(Text, nullable=True)
    processed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    rider = relationship("Rider", back_populates="messages")
    conversation = relationship("Conversation", back_populates="messages")


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(String(50), unique=True, nullable=True, index=True)
    rider_id = Column(String(10), ForeignKey("riders.rider_id"), nullable=False, index=True)
    message_id = Column(GUID(), ForeignKey("messages.id"), nullable=True)
    category = Column(String(50), nullable=True)  # surge_missing, incentive_missing, penalty_duplicate, payment_missing, distance_wrong, etc.
    trip_id = Column(String(20), nullable=True, index=True)
    disputed_date = Column(Date, nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(30), default=DisputeStatus.OPEN.value, nullable=False)

    # Calculated amounts
    expected_amount = Column(Numeric(10, 2), nullable=True)
    actual_amount = Column(Numeric(10, 2), nullable=True)
    shortfall = Column(Numeric(10, 2), nullable=True)

    # Resolution
    resolution_notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    rider = relationship("Rider", back_populates="disputes")
    payment_attempts = relationship("PaymentAttempt", back_populates="dispute")
    approval_request = relationship("ApprovalRequest", back_populates="dispute", uselist=False)
    evidence = relationship("DisputeEvidence", back_populates="dispute")


class DisputeEvidence(Base):
    __tablename__ = "dispute_evidence"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(GUID(), ForeignKey("disputes.id"), nullable=False, index=True)
    evidence_type = Column(String(50), nullable=False)  # trip_record, payout_record, calculation, payswift_check
    data = Column(Text, nullable=False)  # JSON string
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    dispute = relationship("Dispute", back_populates="evidence")


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(GUID(), ForeignKey("disputes.id"), nullable=False, index=True)
    rider_id = Column(String(10), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    idempotency_key = Column(String(100), unique=True, nullable=False)
    payswift_payout_id = Column(String(100), nullable=True)
    payswift_status = Column(String(30), nullable=True)
    status = Column(String(30), default=PaymentStatus.PENDING.value, nullable=False)
    reference = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    dispute = relationship("Dispute", back_populates="payment_attempts")

    __table_args__ = (
        Index("ix_payment_rider_date", "rider_id", "created_at"),
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(GUID(), ForeignKey("disputes.id"), nullable=False, unique=True)
    rider_id = Column(String(10), nullable=False, index=True)
    type = Column(String(20), default=OpsItemType.APPROVAL.value, nullable=False)  # approval or escalation
    amount = Column(Numeric(10, 2), nullable=True)
    reason = Column(Text, nullable=False)
    evidence_summary = Column(Text, nullable=True)
    status = Column(String(20), default=ApprovalStatus.PENDING.value, nullable=False)
    reviewer = Column(String(100), nullable=True)
    decision_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)

    dispute = relationship("Dispute", back_populates="approval_request")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    rider_id = Column(String(10), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)  # message_in, tool_call, decision, reply, error
    name = Column(String(200), nullable=False)
    input_data = Column(Text, nullable=True)  # JSON
    output_data = Column(Text, nullable=True)  # JSON
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_audit_rider_time", "rider_id", "created_at"),
    )
