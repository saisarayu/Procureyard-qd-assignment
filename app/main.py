"""FastAPI application for QuickDrop Rider Payout Dispute Desk."""
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.connection import get_db, engine, Base
import app.models  # Register all models with Base
from app.services.data_loader import DataLoader, load_csv_data
from app.services.payswift_client import PaySwiftClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize database tables on startup."""
    logger.info("QuickDrop application starting up...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified.")
    except Exception as e:
        logger.warning(f"Could not connect to database on startup: {e}")
    yield
    logger.info("QuickDrop application shutting down...")
    await engine.dispose()


app = FastAPI(
    title="QuickDrop Rider Payout Dispute Desk",
    description="Deterministic Payout Engine and Dispute Resolution API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    """Health check endpoint verifying application, database, and PaySwift status."""
    db_healthy = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            db_healthy = True
    except Exception as e:
        logger.warning(f"DB health check failed: {e}")

    payswift_healthy = False
    try:
        async with PaySwiftClient() as client:
            payswift_healthy = await client.health_check()
    except Exception as e:
        logger.warning(f"PaySwift health check failed: {e}")

    overall_healthy = db_healthy  # PaySwift sandbox may be separate, but report its status

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "database": "connected" if db_healthy else "disconnected",
        "payswift": "connected" if payswift_healthy else "unreachable",
        "environment": "production" if not settings.DATABASE_URL.startswith("sqlite") else "local_test",
    }


@app.get("/data-quality", tags=["Data Quality"])
async def get_data_quality_report():
    """Run data quality analysis across repository CSV files and return structured audit report."""
    loader = DataLoader()
    report = loader.inspect_csv_data()
    return {
        "summary": report.summary(),
        "issues_sample": [
            {
                "category": issue.category,
                "severity": issue.severity,
                "entity_type": issue.entity_type,
                "entity_id": issue.entity_id,
                "message": issue.message,
                "details": issue.details,
            }
            for issue in report.issues[:50]
        ],
    }


@app.get("/", tags=["Root"])
async def root():
    return {
        "service": "QuickDrop Rider Payout Dispute Desk",
        "version": "1.0.0",
        "docs_url": "/docs",
        "health_url": "/health",
        "data_quality_url": "/data-quality",
        "dashboard_url": "/dashboard",
    }


# Include Phase 3 Public API and Ops endpoints
from app.api import router as api_router
app.include_router(api_router)

