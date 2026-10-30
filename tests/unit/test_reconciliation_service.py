"""Unit tests for PayoutReconciliationService covering all six known scenarios and edge cases."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.database import Base, Rider, Trip, PayoutLine
from app.services.data_loader import DataLoader
from app.services.reconciliation_service import ReconciliationService, ReconciliationResult

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Create in-memory SQLite database preloaded with the real repository CSV data."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        loader = DataLoader(data_dir=DATA_DIR)
        await loader.load_all(session)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_scenario_a_surge_shortfall(db_session: AsyncSession):
    """Scenario A:
    R003, T926334
    Distance: 6 km, Surge: 1.5
    Expected: ₹74
    Recorded: ₹49
    Shortfall: ₹25
    """
    service = ReconciliationService(db_session)
    result = await service.reconcile_trip_dispute(
        rider_id="R003",
        trip_id="T926334",
        skip_payswift=True,
    )

    assert result.rider_id == "R003"
    assert result.trip_id == "T926334"
    assert result.expected_amount == Decimal("74")
    assert result.recorded_amount == Decimal("49")
    assert result.verified_shortfall == Decimal("25")
    assert result.is_valid_identity is True
    assert result.is_anomalous is False
    assert result.auto_pay_eligible is True  # ₹25 <= ₹200 auto-pay limit


@pytest.mark.asyncio
async def test_scenario_b_surge_shortfall(db_session: AsyncSession):
    """Scenario B:
    R027, T604546
    Distance: 10 km, Surge: 1.2
    Expected: ₹88
    Recorded: ₹73
    Shortfall: ₹15
    """
    service = ReconciliationService(db_session)
    result = await service.reconcile_trip_dispute(
        rider_id="R027",
        trip_id="T604546",
        skip_payswift=True,
    )

    assert result.rider_id == "R027"
    assert result.trip_id == "T604546"
    assert result.expected_amount == Decimal("88")
    assert result.recorded_amount == Decimal("73")
    assert result.verified_shortfall == Decimal("15")
    assert result.is_valid_identity is True
    assert result.is_anomalous is False
    assert result.auto_pay_eligible is True  # ₹15 <= ₹200


@pytest.mark.asyncio
async def test_scenario_c_missing_daily_incentive(db_session: AsyncSession):
    """Scenario C:
    R005, September 21, 2026
    13 completed trips.
    Expected daily incentive: ₹150.
    Recorded incentive: ₹0.
    Shortfall: ₹150.
    """
    service = ReconciliationService(db_session)
    dispute_date = date(2026, 9, 21)
    result = await service.reconcile_daily_incentive_dispute(
        rider_id="R005",
        dispute_date=dispute_date,
        skip_payswift=True,
    )

    assert result.rider_id == "R005"
    assert result.expected_amount == Decimal("150")
    assert result.recorded_amount == Decimal("0")
    assert result.verified_shortfall == Decimal("150")
    assert result.is_anomalous is False
    assert result.auto_pay_eligible is True  # ₹150 <= ₹200


@pytest.mark.asyncio
async def test_scenario_d_duplicate_cancellation_penalty(db_session: AsyncSession):
    """Scenario D:
    R014, September 18, 2026
    Trip T637155 cancelled by rider.
    Duplicate penalty of ₹10 (recorded twice as -10, total -20).
    Expected penalty: -₹10.
    Recorded penalty: -₹20.
    Flag the duplicate and calculate the correction: ₹10 owed back to rider.
    """
    service = ReconciliationService(db_session)
    result = await service.reconcile_trip_dispute(
        rider_id="R014",
        trip_id="T637155",
        skip_payswift=True,
    )

    assert result.rider_id == "R014"
    assert result.trip_id == "T637155"
    assert result.expected_amount == Decimal("-10")
    assert result.recorded_amount == Decimal("-20")
    assert result.verified_shortfall == Decimal("10")  # Rider was over-penalized by ₹10
    assert result.is_anomalous is True  # Flagged duplicate
    assert any("Duplicate cancellation penalties" in flag for flag in result.anomaly_flags)


@pytest.mark.asyncio
async def test_scenario_e_r7_vs_r007_identity_mismatch(db_session: AsyncSession):
    """Scenario E:
    T840677
    Trip rider ID is R7.
    Payout rider ID is R007.
    Trip calculation: Expected fare ₹101, Recorded ₹67, Shortfall ₹34.
    Flag identity mismatch.
    Never merge automatically.
    """
    service = ReconciliationService(db_session)

    # When queried with R007 (as in payout ledger) but trip belongs to R7:
    result = await service.reconcile_trip_dispute(
        rider_id="R007",
        trip_id="T840677",
        skip_payswift=True,
    )

    assert result.expected_amount == Decimal("101")
    assert result.recorded_amount == Decimal("67")
    assert result.verified_shortfall == Decimal("34")
    assert result.is_valid_identity is False
    assert result.is_anomalous is True
    assert any("Rider ID mismatch" in flag for flag in result.anomaly_flags)
    assert result.auto_pay_eligible is False  # Must not auto-pay identity mismatches


@pytest.mark.asyncio
async def test_scenario_e_direct_r7_query(db_session: AsyncSession):
    """Scenario E (alternative): Querying with R7 directly still flags payout line mismatch."""
    service = ReconciliationService(db_session)
    result = await service.reconcile_trip_dispute(
        rider_id="R7",
        trip_id="T840677",
        skip_payswift=True,
    )
    # R7 is not registered in riders table
    assert result.is_valid_identity is False
    assert result.is_anomalous is True
    assert result.auto_pay_eligible is False


@pytest.mark.asyncio
async def test_scenario_f_distance_anomaly_5000km(db_session: AsyncSession):
    """Scenario F:
    T312538, R031
    Distance: 5000 km.
    Flag as anomalous.
    Do not automatically authorize payment.
    """
    service = ReconciliationService(db_session)
    result = await service.reconcile_trip_dispute(
        rider_id="R031",
        trip_id="T312538",
        skip_payswift=True,
    )

    assert result.trip_id == "T312538"
    assert result.is_anomalous is True
    assert any("Anomalous distance" in flag for flag in result.anomaly_flags)
    assert result.auto_pay_eligible is False  # Never automatically pay obviously anomalous distance


@pytest.mark.asyncio
async def test_payswift_prevents_double_payment(db_session: AsyncSession):
    """If PaySwift already disbursed funds for this trip, verified shortfall is reduced."""
    class FakePaySwiftClient:
        async def get_payouts(self, rider_id=None):
            return [
                {"payout_id": "ps_prev", "rider_id": "R003", "amount": 25, "reference": "dispute-T926334-settled"}
            ]

    service = ReconciliationService(db_session, payswift_client=FakePaySwiftClient())
    result = await service.reconcile_trip_dispute(
        rider_id="R003",
        trip_id="T926334",
        skip_payswift=False,
    )

    assert result.expected_amount == Decimal("74")
    assert result.recorded_amount == Decimal("49")
    assert result.payswift_paid_amount == Decimal("25")
    # Shortfall is 0 because PaySwift already settled the ₹25!
    assert result.verified_shortfall == Decimal("0")
    assert result.auto_pay_eligible is False
