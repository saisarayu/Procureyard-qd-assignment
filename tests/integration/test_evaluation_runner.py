"""Test for the evaluation runner against FastAPI app."""
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.agent.llm_provider import FallbackProvider
from app.api.endpoints import orchestrator
from app.database.connection import get_db, Base
from app.evaluation.runner import run_evaluation
from app.main import app
from app.services.data_loader import DataLoader

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@pytest.mark.asyncio
async def test_evaluation_runner_in_process():
    """Verify that evaluation runner can successfully run against FastAPI test client."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        loader = DataLoader(data_dir=DATA_DIR)
        await loader.load_all(session)

    async def override_get_db():
        async with session_maker() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db

    mock_ps = AsyncMock()
    mock_ps.create_payout = AsyncMock(return_value={"payout_id": "ps_eval_01", "status": "paid"})
    mock_ps.verify_transaction = AsyncMock(return_value={"payout_id": "ps_eval_01", "status": "paid"})
    mock_ps.get_payouts = AsyncMock(return_value=[{"payout_id": "ps_eval_01", "status": "paid"}])
    mock_ps.health_check = AsyncMock(return_value=True)

    orig_ps = orchestrator.payswift
    orig_llm = orchestrator.llm
    orchestrator.payswift = mock_ps
    orchestrator.llm = FallbackProvider()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        success = await run_evaluation(base_url="http://test", client=test_client)
        assert success is True

    orchestrator.payswift = orig_ps
    orchestrator.llm = orig_llm
    app.dependency_overrides.clear()
    await engine.dispose()
