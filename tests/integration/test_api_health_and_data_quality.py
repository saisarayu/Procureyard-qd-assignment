"""Integration tests for FastAPI endpoints: /health, /data-quality, /."""
import pytest
import httpx

from app.main import app


@pytest.mark.asyncio
async def test_get_root():
    """Verify GET / returns service info."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "QuickDrop Rider Payout Dispute Desk" in data["service"]
        assert data["health_url"] == "/health"
        assert data["data_quality_url"] == "/data-quality"


@pytest.mark.asyncio
async def test_get_health():
    """Verify GET /health returns status."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "database" in data
        assert "payswift" in data


@pytest.mark.asyncio
async def test_get_data_quality():
    """Verify GET /data-quality returns repository data quality audit report."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/data-quality")
        assert resp.status_code == 200
        data = resp.json()
        summary = data["summary"]
        assert summary["counts"]["riders"]["read"] == 40
        assert summary["counts"]["trips"]["read"] >= 3300
        assert summary["counts"]["payout_lines"]["read"] >= 3200
        assert summary["total_issues"] > 0
        assert len(data["issues_sample"]) > 0
