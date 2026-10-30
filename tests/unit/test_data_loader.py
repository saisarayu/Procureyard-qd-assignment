"""Unit tests for the CSV data loader and quality inspection."""
from decimal import Decimal
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.database import Base, Rider, Trip, PayoutLine
from app.services.data_loader import DataLoader, load_csv_data

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def test_inspect_csv_data_counts_and_quality():
    """Inspect the real CSV files in data/ and verify data quality findings."""
    loader = DataLoader(data_dir=DATA_DIR)
    report = loader.inspect_csv_data()

    # Verify counts
    assert report.total_riders_read == 40
    assert report.total_trips_read >= 3300
    assert report.total_payout_lines_read >= 3200

    # Verify key anomalies are identified
    issues_by_cat = report.issues_by_category

    # Scenario E: R7 vs R007 mismatch
    assert "rider_id_mismatch" in issues_by_cat or "rider_mismatch" in issues_by_cat
    r7_issues = [i for i in report.issues if "R7" in str(i.details) or "R7" in i.message]
    assert len(r7_issues) > 0

    # Scenario F: 5000 km anomaly (T312538)
    assert "distance_anomaly" in issues_by_cat
    anomaly_5000 = [i for i in report.issues if i.category == "distance_anomaly" and "5000" in i.message]
    assert len(anomaly_5000) > 0
    assert any(i.entity_id == "T312538" for i in anomaly_5000)

    # Scenario D: Duplicate cancellation penalty
    assert "duplicate_cancellation_penalty" in issues_by_cat
    dup_penalties = [i for i in report.issues if i.category == "duplicate_cancellation_penalty" and "T637155" in str(i.details)]
    assert len(dup_penalties) > 0


@pytest.mark.asyncio
async def test_idempotent_loading_with_sqlite():
    """Test that importing data twice does not duplicate records (idempotent)."""
    # Create in-memory SQLite engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # First load
    async with async_session() as session:
        loader = DataLoader(data_dir=DATA_DIR)
        report1 = await loader.load_all(session)
        assert report1.total_riders_imported == 40
        assert report1.total_trips_imported > 3000
        assert report1.total_payout_lines_imported > 3000

    # Second load (restart simulation)
    async with async_session() as session:
        loader2 = DataLoader(data_dir=DATA_DIR)
        report2 = await loader2.load_all(session)
        # Should import 0 new records because they already exist!
        assert report2.total_riders_imported == 0
        assert report2.total_trips_imported == 0
        assert report2.total_payout_lines_imported == 0

    await engine.dispose()
