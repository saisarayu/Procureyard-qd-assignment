"""Unit tests for database integrity: foreign keys, unique constraints,
indexes, rollback handling, and duplicate payment prevention.
"""
import uuid
from datetime import datetime, date, timezone
from decimal import Decimal
import pytest
import pytest_asyncio
from sqlalchemy import select, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.database import (
    Base, Rider, Trip, PayoutLine, Conversation, Message,
    Dispute, DisputeEvidence, PaymentAttempt, ApprovalRequest,
    DisputeStatus, PaymentStatus, ApprovalStatus, OpsItemType
)


@pytest_asyncio.fixture
async def sqlite_engine():
    """Create in-memory SQLite engine with foreign keys enabled."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Enable foreign keys in SQLite
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraints_and_idempotency_key(sqlite_engine):
    """Verify unique constraint on PaymentAttempt.idempotency_key prevents duplicate payments."""
    session_maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async with session_maker() as session:
        # Create rider
        rider = Rider(rider_id="R999", name="Test Rider", city="Mumbai", joined_on=date(2026, 1, 1))
        session.add(rider)
        await session.flush()

        # Create dispute
        dispute = Dispute(
            id=uuid.uuid4(),
            rider_id="R999",
            status=DisputeStatus.INVESTIGATING.value,
        )
        session.add(dispute)
        await session.flush()

        # Insert first payment attempt
        idempotency_key = "idem-test-key-12345"
        attempt1 = PaymentAttempt(
            id=uuid.uuid4(),
            dispute_id=dispute.id,
            rider_id="R999",
            amount=Decimal("100.00"),
            idempotency_key=idempotency_key,
            status=PaymentStatus.PROCESSING.value,
        )
        session.add(attempt1)
        await session.commit()

    # Attempt to insert second payment with identical idempotency_key must raise IntegrityError
    async with session_maker() as session:
        attempt2 = PaymentAttempt(
            id=uuid.uuid4(),
            dispute_id=dispute.id,
            rider_id="R999",
            amount=Decimal("100.00"),
            idempotency_key=idempotency_key,
            status=PaymentStatus.PROCESSING.value,
        )
        session.add(attempt2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_duplicate_message_prevention(sqlite_engine):
    """Verify unique constraint on Message.message_id prevents duplicate message ingestion."""
    session_maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async with session_maker() as session:
        rider = Rider(rider_id="R998", name="Rider Msg", city="Delhi", joined_on=date(2026, 1, 1))
        session.add(rider)
        await session.flush()

        msg1 = Message(
            id=uuid.uuid4(),
            message_id="msg_external_1001",
            rider_id="R998",
            role="rider",
            text="Paisa kam mila",
            received_at=datetime.now(timezone.utc),
        )
        session.add(msg1)
        await session.commit()

    async with session_maker() as session:
        msg_duplicate = Message(
            id=uuid.uuid4(),
            message_id="msg_external_1001",
            rider_id="R998",
            role="rider",
            text="Paisa kam mila duplicate",
            received_at=datetime.now(timezone.utc),
        )
        session.add(msg_duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_transaction_rollback_preserves_state(sqlite_engine):
    """Verify that an aborted transaction rolls back all partial operations cleanly."""
    session_maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async with session_maker() as session:
        rider = Rider(rider_id="R997", name="Rollback Rider", city="Bengaluru", joined_on=date(2026, 1, 1))
        session.add(rider)
        await session.commit()

    # Begin failed transaction
    async with session_maker() as session:
        # Add a dispute
        dispute = Dispute(
            id=uuid.uuid4(),
            rider_id="R997",
            status=DisputeStatus.OPEN.value,
        )
        session.add(dispute)
        await session.flush()

        # Intentionally cause an error by adding duplicate rider ID
        duplicate_rider = Rider(rider_id="R997", name="Duplicate", city="Pune", joined_on=date(2026, 1, 1))
        session.add(duplicate_rider)
        with pytest.raises(IntegrityError):
            await session.commit()

    # Verify dispute was NOT saved due to rollback
    async with session_maker() as session:
        result = await session.execute(select(Dispute).where(Dispute.rider_id == "R997"))
        disputes = result.scalars().all()
        assert len(disputes) == 0


@pytest.mark.asyncio
async def test_foreign_key_enforcement(sqlite_engine):
    """Verify foreign key constraint: cannot create conversation for nonexistent rider."""
    session_maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async with session_maker() as session:
        conv = Conversation(
            id=uuid.uuid4(),
            conversation_id="conv_nonexistent_rider",
            rider_id="R_DOES_NOT_EXIST",
            status="active",
        )
        session.add(conv)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_trip_primary_key_duplicate_prevention(sqlite_engine):
    """Verify primary key constraint on trips table prevents duplicate trip IDs."""
    session_maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async with session_maker() as session:
        trip1 = Trip(
            trip_id="T_DUP_TEST_01",
            rider_id="R001",
            started_at=datetime.now(timezone.utc),
            status="completed",
            distance_km=Decimal("5.0"),
            surge_multiplier=Decimal("1.0"),
        )
        session.add(trip1)
        await session.commit()

    async with session_maker() as session:
        trip2 = Trip(
            trip_id="T_DUP_TEST_01",
            rider_id="R002",
            started_at=datetime.now(timezone.utc),
            status="completed",
            distance_km=Decimal("8.0"),
            surge_multiplier=Decimal("1.2"),
        )
        session.add(trip2)
        with pytest.raises(IntegrityError):
            await session.commit()
