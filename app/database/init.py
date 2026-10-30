"""CLI script to initialize the database and load source CSV data.

Usage:
    python -m app.database.init
    python -m app.database.init --url sqlite+aiosqlite:///quickdrop.db
    python -m app.database.init --sqlite
"""
import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import get_settings
from app.database.connection import engine as default_engine, Base
import app.models  # Ensure all models are registered
from app.services.data_loader import load_csv_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def run_init(database_url: str):
    logger.info(f"Connecting to database: {database_url}")
    engine_kwargs = {"echo": False}
    if "sqlite" not in database_url:
        engine_kwargs.update({
            "pool_size": 10,
            "max_overflow": 20,
            "pool_pre_ping": True,
        })

    engine = create_async_engine(database_url, **engine_kwargs)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    logger.info("Initializing database schema...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema created successfully.")

    logger.info("Loading CSV data and performing data quality audit...")
    async with session_factory() as session:
        report = await load_csv_data(session)

    print("\n" + "=" * 65)
    print(" QUICKDROP DATA IMPORT & QUALITY AUDIT REPORT")
    print("=" * 65)
    print(f" Riders:       {report.total_riders_read:>5} read  |  {report.total_riders_imported:>5} newly imported")
    print(f" Trips:        {report.total_trips_read:>5} read  |  {report.total_trips_imported:>5} newly imported")
    print(f" Payout Lines: {report.total_payout_lines_read:>5} read  |  {report.total_payout_lines_imported:>5} newly imported")
    print("-" * 65)
    print(f" Total Quality Issues Flagged: {len(report.issues)}")
    print(" Issues by Category:")
    for cat, count in report.issues_by_category.items():
        print(f"   • {cat:<32}: {count}")
    print("-" * 65)
    print(" Notable Issues Sample (first 10):")
    for issue in report.issues[:10]:
        print(f"   [{issue.severity:<7}] {issue.category:<28} ({issue.entity_type} {issue.entity_id}): {issue.message}")
    if len(report.issues) > 10:
        print(f"   ... and {len(report.issues) - 10} more issues logged.")
    print("=" * 65 + "\n")

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Initialize QuickDrop database and import CSV files.")
    parser.add_argument("--url", type=str, default=None, help="Database URL (defaults to env DATABASE_URL)")
    parser.add_argument("--sqlite", action="store_true", help="Use local SQLite database (quickdrop.db)")

    args = parser.parse_args()

    if args.sqlite:
        db_url = "sqlite+aiosqlite:///quickdrop.db"
    elif args.url:
        db_url = args.url
    else:
        # Check env var or settings
        db_url = os.getenv("DATABASE_URL") or get_settings().DATABASE_URL

    try:
        asyncio.run(run_init(db_url))
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        if "getaddrinfo failed" in str(e) or "Connection refused" in str(e):
            logger.info("Hint: To test locally with SQLite, run: python -m app.database.init --sqlite")
        sys.exit(1)


if __name__ == "__main__":
    main()
