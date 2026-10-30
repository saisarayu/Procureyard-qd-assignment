"""Comprehensive End-to-End and Integration Tests for Phase 3 Agent Workflow.

Covers all 17 required scenarios:
1. Valid trip shortfall
2. Missing daily incentive
3. Duplicate cancellation penalty
4. Identity mismatch R7 versus R007
5. Anomalous 5000 km trip
6. Dispute outside seven-day window
7. Dispute exceeding ₹200
8. Repeat dispute requiring approval
9. Duplicate incoming message_id
10. Concurrent duplicate message requests
11. PaySwift timeout
12. PaySwift successful payment and verification
13. PaySwift ambiguous response
14. Approval and rejection workflows
15. Invalid rider
16. Unclear complaint requiring clarification
17. LLM unavailable with deterministic fallback
"""
import asyncio
from datetime import datetime, date, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select, and_, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.agent.llm_provider import FallbackProvider
from app.api.endpoints import orchestrator
from app.database.connection import get_db, Base
from app.main import app
from app.models.database import (
    Rider, Trip, PayoutLine, Message, Dispute, PaymentAttempt, ApprovalRequest,
    DisputeStatus, PaymentStatus, ApprovalStatus
)
from app.services.data_loader import DataLoader
from app.services.payswift_client import PaySwiftClient, PaySwiftTimeout, PaySwiftUnavailable

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@pytest_asyncio.fixture(scope="function")
async def test_db_engine():
    """Create in-memory SQLite database loaded with real repository CSV data."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        loader = DataLoader(data_dir=DATA_DIR)
        await loader.load_all(session)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(test_db_engine):
    """FastAPI AsyncClient overriding get_db to use test_db_engine."""
    session_maker = async_sessionmaker(test_db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Default mocked PaySwift client that simulates successful payments
    mock_ps = AsyncMock()
    mock_ps.create_payout = AsyncMock(return_value={"payout_id": "ps_mock_123", "status": "paid"})
    mock_ps.verify_transaction = AsyncMock(return_value={"payout_id": "ps_mock_123", "status": "paid"})
    mock_ps.get_payouts = AsyncMock(return_value=[{"payout_id": "ps_mock_123", "status": "paid"}])
    mock_ps.health_check = AsyncMock(return_value=True)

    original_payswift = orchestrator.payswift
    original_llm = orchestrator.llm
    orchestrator.payswift = mock_ps
    orchestrator.llm = FallbackProvider()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c

    orchestrator.payswift = original_payswift
    orchestrator.llm = original_llm
    app.dependency_overrides.clear()


# ── Test 1: Valid trip shortfall (Scenario A) ─────────────────────────

@pytest.mark.asyncio
async def test_01_valid_trip_shortfall(client: httpx.AsyncClient):
    """Scenario A: R003, T926334 shortfall of ₹25 auto-paid."""
    payload = {
        "message_id": "wamid.test_01",
        "rider_id": "R003",
        "text": "Check trip T926334, payment is short.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "₹74" in data["reply"]
    assert "₹49" in data["reply"]
    assert "₹25" in data["reply"]
    assert data["message_id"] == "wamid.test_01"

    # Verify audit trace
    trace_res = await client.get("/trace/R003")
    assert trace_res.status_code == 200
    trace = trace_res.json()
    assert any(t["name"] == "execute_payswift_payment" for t in trace)


# ── Test 2: Missing daily incentive (Scenario C) ──────────────────────

@pytest.mark.asyncio
async def test_02_missing_daily_incentive(client: httpx.AsyncClient):
    """Scenario C: R005 completed 13 trips on 2026-09-21, incentive ₹150 auto-paid."""
    payload = {
        "message_id": "wamid.test_02",
        "rider_id": "R005",
        "text": "Kal 21 tarikh ko 13 trip maare the par incentive nahi mila.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "150" in data["reply"]
    assert "PaySwift" in data["reply"] or "verified" in data["reply"]


# ── Test 3: Duplicate cancellation penalty (Scenario D) ───────────────

@pytest.mark.asyncio
async def test_03_duplicate_cancellation_penalty(client: httpx.AsyncClient):
    """Scenario D: R014 on T637155 was penalized twice (-₹10 and -₹10).
    Agent flags duplicate penalty and forwards refund request to ops.
    """
    payload = {
        "message_id": "wamid.test_03",
        "rider_id": "R014",
        "text": "Trip T637155 cancelled hua tha lekin do baar penalty kat gaya.",
        "received_at": "2026-09-20T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "duplicate" in data["reply"].lower()
    assert "operations" in data["reply"].lower() or "approval" in data["reply"].lower()

    # Check pending ops
    ops_res = await client.get("/ops/pending")
    items = ops_res.json()
    assert any(i["rider_id"] == "R014" for i in items)


# ── Test 4: Identity mismatch R7 vs R007 (Scenario E) ─────────────────

@pytest.mark.asyncio
async def test_04_identity_mismatch_r7_vs_r007(client: httpx.AsyncClient):
    """Scenario E: R007 claims trip T840677 (which belongs to R7).
    Must NEVER merge or auto-pay! Escalates to ops.
    """
    payload = {
        "message_id": "wamid.test_04",
        "rider_id": "R007",
        "text": "My payment for trip T840677 is short, please check.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "mismatch" in data["reply"].lower()
    assert "operations" in data["reply"].lower()

    # Verify an escalation was created
    ops_res = await client.get("/ops/pending")
    items = ops_res.json()
    assert any(i["rider_id"] == "R007" and i["type"] == "escalation" for i in items)


# ── Test 5: Anomalous 5000 km trip (Scenario F) ───────────────────────

@pytest.mark.asyncio
async def test_05_anomalous_5000km_trip(client: httpx.AsyncClient):
    """Scenario F: R031 on trip T312538 has distance 5000 km.
    Flagged as anomalous, never auto-paid, escalated.
    """
    payload = {
        "message_id": "wamid.test_05",
        "rider_id": "R031",
        "text": "Please check trip T312538 payout.",
        "received_at": "2026-09-20T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "unusual distance" in data["reply"].lower() or "anomalous" in data["reply"].lower()

    ops_res = await client.get("/ops/pending")
    items = ops_res.json()
    assert any(i["rider_id"] == "R031" and i["type"] == "escalation" for i in items)


# ── Test 6: Dispute outside seven-day window ─────────────────────────

@pytest.mark.asyncio
async def test_06_dispute_outside_seven_day_window(client: httpx.AsyncClient):
    """Trip from 2026-09-12 disputed on 2026-09-22 (> 7 days). Must be rejected."""
    payload = {
        "message_id": "wamid.test_06",
        "rider_id": "R003",
        "text": "My trip on 2026-09-12 has incorrect payout.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "7-day" in data["reply"] or "outside" in data["reply"].lower()


# ── Test 7: Dispute exceeding ₹200 ────────────────────────────────────

@pytest.mark.asyncio
async def test_07_dispute_exceeding_200(client: httpx.AsyncClient, test_db_engine):
    """Dispute where shortfall > ₹200 requires human approval and is not auto-paid."""
    # Insert a custom trip with ₹300 shortfall for R010
    session_maker = async_sessionmaker(test_db_engine, expire_on_commit=False)
    async with session_maker() as session:
        trip = Trip(
            trip_id="T999991",
            rider_id="R010",
            started_at=datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc),
            status="completed",
            distance_km=Decimal("40.0"),  # 25 + 38*6 = 253, surged x1.5 = 380
            surge_multiplier=Decimal("1.5"),
        )
        payout = PayoutLine(
            line_id="PL999991",
            payout_date=date(2026, 9, 21),
            rider_id="R010",
            line_type="trip",
            trip_id="T999991",
            amount=Decimal("50.00"),  # Shortfall: 380 - 50 = 330 > 200
        )
        session.add_all([trip, payout])
        await session.commit()

    payload = {
        "message_id": "wamid.test_07",
        "rider_id": "R010",
        "text": "Please check trip T999991, payout is short.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "exceeds" in data["reply"].lower() or "operations" in data["reply"].lower()

    # Verify pending approval in ops
    ops_res = await client.get("/ops/pending")
    items = ops_res.json()
    assert any(i["rider_id"] == "R010" and i["type"] == "approval" for i in items)


# ── Test 8: Repeat dispute requiring approval ─────────────────────────

@pytest.mark.asyncio
async def test_08_repeat_dispute_requiring_approval(client: httpx.AsyncClient):
    """Repeat dispute for already processed trip T926334 requires human approval."""
    # First message: auto-pays ₹25
    payload1 = {
        "message_id": "wamid.test_08_first",
        "rider_id": "R003",
        "text": "Check trip T926334 payout.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp1 = await client.post("/messages", json=payload1)
    assert resp1.status_code == 200

    # Second message: new message_id but same trip dispute
    payload2 = {
        "message_id": "wamid.test_08_second",
        "rider_id": "R003",
        "text": "Check trip T926334 again please.",
        "received_at": "2026-09-22T10:05:00Z"
    }
    resp2 = await client.post("/messages", json=payload2)
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Repeat dispute requires human review, not auto-pay
    assert "operations" in data2["reply"].lower() or "review" in data2["reply"].lower() or "approval" in data2["reply"].lower() or "forwarded" in data2["reply"].lower()


# ── Test 9: Duplicate incoming message_id (Idempotency) ───────────────

@pytest.mark.asyncio
async def test_09_duplicate_incoming_message_id(client: httpx.AsyncClient):
    """Duplicate message_id returns exact same reply without double payment."""
    payment_call_count = orchestrator.payswift.create_payout.call_count
    payload = {
        "message_id": "wamid.idem_test_09",
        "rider_id": "R027",
        "text": "Check trip T604546.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp1 = await client.post("/messages", json=payload)
    assert resp1.status_code == 200
    reply1 = resp1.json()["reply"]

    # Send exact same request again
    resp2 = await client.post("/messages", json=payload)
    assert resp2.status_code == 200
    reply2 = resp2.json()["reply"]

    assert reply1 == reply2
    assert orchestrator.payswift.create_payout.call_count - payment_call_count == 1


# ── Test 10: Concurrent duplicate message requests ────────────────────

@pytest.mark.asyncio
async def test_10_concurrent_duplicate_message_requests(client: httpx.AsyncClient):
    """Concurrent identical requests are safely resolved by database unique constraints."""
    payment_call_count = orchestrator.payswift.create_payout.call_count
    payload = {
        "message_id": "wamid.concurrent_10",
        "rider_id": "R003",
        "text": "Check trip T926334.",
        "received_at": "2026-09-22T10:00:00Z"
    }

    # Launch two concurrent post requests
    r1, r2 = await asyncio.gather(
        client.post("/messages", json=payload),
        client.post("/messages", json=payload),
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["reply"] == r2.json()["reply"]
    assert orchestrator.payswift.create_payout.call_count - payment_call_count == 1


# ── Test 11: PaySwift timeout ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_11_payswift_timeout(client: httpx.AsyncClient):
    """PaySwift timeout moves transaction to verification_required/pending_approval."""
    orchestrator.payswift.create_payout = AsyncMock(side_effect=PaySwiftTimeout("Request timed out"))

    payload = {
        "message_id": "wamid.timeout_11",
        "rider_id": "R003",
        "text": "Check trip T926334.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "operations" in data["reply"].lower() or "review" in data["reply"].lower()


# ── Test 12: PaySwift successful payment and verification ──────────────

@pytest.mark.asyncio
async def test_12_payswift_successful_payment_and_verification(client: httpx.AsyncClient):
    """Payment succeeds and verification check verifies transaction."""
    orchestrator.payswift.create_payout = AsyncMock(return_value={"payout_id": "ps_success_777", "status": "paid"})
    orchestrator.payswift.verify_transaction = AsyncMock(return_value={"payout_id": "ps_success_777", "status": "paid"})

    payload = {
        "message_id": "wamid.verify_12",
        "rider_id": "R027",
        "text": "Check trip T604546.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "₹15" in data["reply"]
    assert "PaySwift" in data["reply"]


# ── Test 13: PaySwift ambiguous response ──────────────────────────────

@pytest.mark.asyncio
async def test_13_payswift_ambiguous_response(client: httpx.AsyncClient):
    """PaySwift 503 Service Unavailable is handled cleanly and marked for ops review."""
    orchestrator.payswift.create_payout = AsyncMock(side_effect=PaySwiftUnavailable("Gateway 503"))

    payload = {
        "message_id": "wamid.ambig_13",
        "rider_id": "R003",
        "text": "Check trip T926334.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "operations" in data["reply"].lower() or "review" in data["reply"].lower()


# ── Test 14: Approval and Rejection workflows ─────────────────────────

@pytest.mark.asyncio
async def test_14_approval_and_rejection_workflows(client: httpx.AsyncClient, test_db_engine):
    """Test POST /ops/approvals/{id}/approve and /reject."""
    # First create an approval item via dispute exceeding limit
    session_maker = async_sessionmaker(test_db_engine, expire_on_commit=False)
    async with session_maker() as session:
        dispute = Dispute(
            rider_id="R003",
            category="large_shortfall",
            shortfall=Decimal("250.00"),
            status=DisputeStatus.PENDING_APPROVAL.value,
            resolution_notes="Dispute exceeds ₹200 limit",
        )
        session.add(dispute)
        await session.flush()

        appr = ApprovalRequest(
            dispute_id=dispute.id,
            rider_id="R003",
            type="approval",
            amount=Decimal("250.00"),
            reason="Exceeds ₹200",
            status=ApprovalStatus.PENDING.value,
        )
        session.add(appr)
        await session.commit()
        appr_id = str(appr.id)

    # 1. Verify unauthorized request is rejected with 401
    unauth_resp = await client.post(f"/ops/approvals/{appr_id}/approve", json={"reviewer": "anonymous"})
    assert unauth_resp.status_code == 401

    # 2. Approve request with valid operations auth
    appr_resp = await client.post(
        f"/ops/approvals/{appr_id}/approve",
        json={"reviewer": "ops_test", "notes": "Approved in test"},
        auth=("ops", "quickdrop2026"),
    )
    assert appr_resp.status_code == 200
    data = appr_resp.json()
    assert data["status"] == "approved"
    assert "disbursed successfully" in data["message"]

    # 3. Trying to approve again returns already approved message
    dup_appr = await client.post(
        f"/ops/approvals/{appr_id}/approve",
        json={"reviewer": "ops_test"},
        auth=("ops", "quickdrop2026"),
    )
    assert dup_appr.status_code == 200
    assert "already in 'approved'" in dup_appr.json()["message"]

    # 4. Test rejection on a second item (with auth)
    async with session_maker() as session:
        disp2 = Dispute(
            rider_id="R003",
            category="suspect",
            shortfall=Decimal("50.00"),
            status=DisputeStatus.PENDING_APPROVAL.value,
        )
        session.add(disp2)
        await session.flush()
        appr2 = ApprovalRequest(
            dispute_id=disp2.id,
            rider_id="R003",
            type="approval",
            amount=Decimal("50.00"),
            reason="Suspect claim",
            status=ApprovalStatus.PENDING.value,
        )
        session.add(appr2)
        await session.commit()
        appr2_id = str(appr2.id)

    # Reject without auth must return 401
    unauth_rej = await client.post(f"/ops/approvals/{appr2_id}/reject", json={"reviewer": "bad_actor"})
    assert unauth_rej.status_code == 401

    # Reject with valid auth must succeed
    rej_resp = await client.post(
        f"/ops/approvals/{appr2_id}/reject",
        json={"reviewer": "ops_test", "notes": "Fraud detected"},
        auth=("ops", "quickdrop2026"),
    )
    assert rej_resp.status_code == 200
    assert rej_resp.json()["status"] == "rejected"


# ── Test 15: Invalid rider ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_invalid_rider(client: httpx.AsyncClient):
    """Rider not in database receives polite error message."""
    payload = {
        "message_id": "wamid.invalid_15",
        "rider_id": "R_NON_EXISTENT",
        "text": "Where is my money?",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "couldn't find rider" in data["reply"].lower() or "support" in data["reply"].lower()


# ── Test 16: Unclear complaint requiring clarification ────────────────

@pytest.mark.asyncio
async def test_16_unclear_complaint_requiring_clarification(client: httpx.AsyncClient):
    """Vague message with no trip ID or date asks clarification question."""
    payload = {
        "message_id": "wamid.unclear_16",
        "rider_id": "R003",
        "text": "Paisa kam aaya hai mera.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "?" in data["reply"] or "order" in data["reply"].lower() or "trip" in data["reply"].lower() or "date" in data["reply"].lower()


# ── Test 17: LLM unavailable with deterministic fallback ─────────────

@pytest.mark.asyncio
async def test_17_llm_unavailable_deterministic_fallback(client: httpx.AsyncClient):
    """When LLM provider raises an exception, system falls back deterministically."""
    orchestrator.llm.chat_completion = AsyncMock(side_effect=RuntimeError("Groq 500 Internal Server Error"))

    payload = {
        "message_id": "wamid.fallback_17",
        "rider_id": "R003",
        "text": "Check trip T926334 surge payout.",
        "received_at": "2026-09-22T10:00:00Z"
    }
    resp = await client.post("/messages", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "T926334" in data["reply"]
    assert "25" in data["reply"]
