"""Reusable, idempotent CSV data loader and comprehensive data quality validator.

Validates and imports:
- data/riders.csv
- data/trips.csv
- data/payout_lines.csv

Features:
- Preserves original IDs exactly (never normalizes R7 to R007).
- Preserves UTC timestamps for trips.
- Validates required columns, formats, and values.
- Identifies data quality issues (anomalies, duplicates, orphans, mismatches).
- Avoids duplicate imports when restarted (fully idempotent).
- Produces a structured data quality report.
"""
import csv
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Rider, Trip, PayoutLine
from app.services.payout_calculator import MAX_PLAUSIBLE_DISTANCE_KM, to_decimal

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@dataclass
class QualityIssue:
    category: str
    severity: str  # "ERROR", "WARNING", "ANOMALY"
    entity_type: str  # "rider", "trip", "payout_line", "cross_reference"
    entity_id: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataQualityReport:
    total_riders_read: int = 0
    total_riders_imported: int = 0
    total_trips_read: int = 0
    total_trips_imported: int = 0
    total_payout_lines_read: int = 0
    total_payout_lines_imported: int = 0
    issues: List[QualityIssue] = field(default_factory=list)

    @property
    def issues_by_category(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for issue in self.issues:
            counts[issue.category] = counts.get(issue.category, 0) + 1
        return counts

    def summary(self) -> Dict[str, Any]:
        return {
            "counts": {
                "riders": {"read": self.total_riders_read, "imported": self.total_riders_imported},
                "trips": {"read": self.total_trips_read, "imported": self.total_trips_imported},
                "payout_lines": {"read": self.total_payout_lines_read, "imported": self.total_payout_lines_imported},
            },
            "total_issues": len(self.issues),
            "issues_by_category": self.issues_by_category,
        }


class DataLoader:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or DATA_DIR
        self.report = DataQualityReport()

    def inspect_csv_data(self) -> DataQualityReport:
        """Inspect all CSV files and generate data quality report without database writes."""
        report = DataQualityReport()

        riders_file = self.data_dir / "riders.csv"
        trips_file = self.data_dir / "trips.csv"
        payout_file = self.data_dir / "payout_lines.csv"

        rider_ids: Set[str] = set()
        trip_ids: Set[str] = set()
        payout_line_ids: Set[str] = set()

        trips_by_id: Dict[str, Dict[str, Any]] = {}
        trips_by_rider_and_date: Dict[Tuple[str, str], int] = {}
        payouts_by_trip: Dict[str, List[Dict[str, Any]]] = {}
        incentives_by_rider_date: Dict[Tuple[str, str], int] = {}
        penalties_by_trip: Dict[str, int] = {}

        # 1. Inspect Riders
        if riders_file.exists():
            with open(riders_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                expected_cols = {"rider_id", "name", "city", "joined_on"}
                if not expected_cols.issubset(reader.fieldnames or []):
                    report.issues.append(QualityIssue(
                        category="schema_error",
                        severity="ERROR",
                        entity_type="rider",
                        entity_id="riders.csv",
                        message=f"Missing required columns in riders.csv. Expected: {expected_cols}, Got: {reader.fieldnames}"
                    ))

                for row_idx, row in enumerate(reader, start=2):
                    report.total_riders_read += 1
                    r_id = row.get("rider_id", "").strip()
                    if not r_id:
                        report.issues.append(QualityIssue(
                            category="missing_value",
                            severity="ERROR",
                            entity_type="rider",
                            entity_id=f"row_{row_idx}",
                            message="Empty rider_id found in riders.csv"
                        ))
                        continue

                    if r_id in rider_ids:
                        report.issues.append(QualityIssue(
                            category="duplicate_record",
                            severity="ERROR",
                            entity_type="rider",
                            entity_id=r_id,
                            message=f"Duplicate rider_id '{r_id}' in riders.csv"
                        ))
                    rider_ids.add(r_id)

                    try:
                        date.fromisoformat(row.get("joined_on", "").strip())
                    except Exception as e:
                        report.issues.append(QualityIssue(
                            category="invalid_format",
                            severity="ERROR",
                            entity_type="rider",
                            entity_id=r_id,
                            message=f"Invalid joined_on date: {row.get('joined_on')} ({e})"
                        ))

        # 2. Inspect Trips
        if trips_file.exists():
            with open(trips_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row_idx, row in enumerate(reader, start=2):
                    report.total_trips_read += 1
                    t_id = row.get("trip_id", "").strip()
                    r_id = row.get("rider_id", "").strip()

                    if not t_id:
                        report.issues.append(QualityIssue(
                            category="missing_value",
                            severity="ERROR",
                            entity_type="trip",
                            entity_id=f"row_{row_idx}",
                            message="Empty trip_id in trips.csv"
                        ))
                        continue

                    if t_id in trip_ids:
                        report.issues.append(QualityIssue(
                            category="duplicate_record",
                            severity="ERROR",
                            entity_type="trip",
                            entity_id=t_id,
                            message=f"Duplicate trip_id '{t_id}' in trips.csv"
                        ))
                    trip_ids.add(t_id)

                    # Check rider reference
                    if r_id not in rider_ids:
                        report.issues.append(QualityIssue(
                            category="rider_mismatch",
                            severity="WARNING",
                            entity_type="trip",
                            entity_id=t_id,
                            message=f"Trip references unknown rider_id '{r_id}' (not in riders.csv)",
                            details={"trip_id": t_id, "rider_id": r_id}
                        ))

                    # Parse distance
                    try:
                        dist = Decimal(row.get("distance_km", "0").strip())
                        if dist < Decimal("0"):
                            report.issues.append(QualityIssue(
                                category="invalid_distance",
                                severity="ERROR",
                                entity_type="trip",
                                entity_id=t_id,
                                message=f"Negative distance '{dist}' km for trip {t_id}",
                                details={"distance_km": str(dist)}
                            ))
                        elif dist > MAX_PLAUSIBLE_DISTANCE_KM:
                            report.issues.append(QualityIssue(
                                category="distance_anomaly",
                                severity="ANOMALY",
                                entity_type="trip",
                                entity_id=t_id,
                                message=f"Anomalous distance {dist} km for trip {t_id} (exceeds {MAX_PLAUSIBLE_DISTANCE_KM} km)",
                                details={"distance_km": str(dist), "rider_id": r_id}
                            ))
                    except (InvalidOperation, ValueError):
                        report.issues.append(QualityIssue(
                            category="invalid_distance",
                            severity="ERROR",
                            entity_type="trip",
                            entity_id=t_id,
                            message=f"Unparseable distance '{row.get('distance_km')}' for trip {t_id}"
                        ))

                    # Parse surge
                    try:
                        surge = Decimal(row.get("surge_multiplier", "1.0").strip())
                        if surge <= Decimal("0"):
                            report.issues.append(QualityIssue(
                                category="invalid_surge",
                                severity="ERROR",
                                entity_type="trip",
                                entity_id=t_id,
                                message=f"Invalid non-positive surge multiplier '{surge}' for trip {t_id}",
                                details={"surge_multiplier": str(surge)}
                            ))
                    except (InvalidOperation, ValueError):
                        report.issues.append(QualityIssue(
                            category="invalid_surge",
                            severity="ERROR",
                            entity_type="trip",
                            entity_id=t_id,
                            message=f"Unparseable surge '{row.get('surge_multiplier')}' for trip {t_id}"
                        ))

                    trips_by_id[t_id] = row

        # 3. Inspect Payout Lines
        if payout_file.exists():
            with open(payout_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row_idx, row in enumerate(reader, start=2):
                    report.total_payout_lines_read += 1
                    line_id = row.get("line_id", "").strip()
                    r_id = row.get("rider_id", "").strip()
                    line_type = row.get("line_type", "").strip()
                    t_id = row.get("trip_id", "").strip() or None
                    p_date = row.get("payout_date", "").strip()

                    if not line_id:
                        report.issues.append(QualityIssue(
                            category="missing_value",
                            severity="ERROR",
                            entity_type="payout_line",
                            entity_id=f"row_{row_idx}",
                            message="Empty line_id in payout_lines.csv"
                        ))
                        continue

                    if line_id in payout_line_ids:
                        report.issues.append(QualityIssue(
                            category="duplicate_record",
                            severity="ERROR",
                            entity_type="payout_line",
                            entity_id=line_id,
                            message=f"Duplicate line_id '{line_id}' in payout_lines.csv"
                        ))
                    payout_line_ids.add(line_id)

                    # Check rider reference
                    if r_id not in rider_ids:
                        report.issues.append(QualityIssue(
                            category="rider_mismatch",
                            severity="WARNING",
                            entity_type="payout_line",
                            entity_id=line_id,
                            message=f"Payout line references unknown rider_id '{r_id}'",
                            details={"line_id": line_id, "rider_id": r_id}
                        ))

                    # Check multiple daily incentive entries
                    if line_type == "daily_incentive":
                        key = (r_id, p_date)
                        incentives_by_rider_date[key] = incentives_by_rider_date.get(key, 0) + 1
                        if incentives_by_rider_date[key] > 1:
                            report.issues.append(QualityIssue(
                                category="duplicate_daily_incentive",
                                severity="WARNING",
                                entity_type="payout_line",
                                entity_id=line_id,
                                message=f"Multiple daily incentive lines for rider '{r_id}' on {p_date}",
                                details={"rider_id": r_id, "date": p_date, "count": incentives_by_rider_date[key]}
                            ))

                    # Check trip references and cancellation penalties
                    if t_id:
                        if t_id not in trip_ids:
                            report.issues.append(QualityIssue(
                                category="orphan_payout_line",
                                severity="WARNING",
                                entity_type="payout_line",
                                entity_id=line_id,
                                message=f"Payout line references nonexistent trip '{t_id}'",
                                details={"line_id": line_id, "trip_id": t_id}
                            ))
                        else:
                            trip_data = trips_by_id[t_id]
                            # Check rider ID match between trip and payout line
                            trip_rider = trip_data.get("rider_id", "").strip()
                            if trip_rider != r_id:
                                report.issues.append(QualityIssue(
                                    category="rider_id_mismatch",
                                    severity="ANOMALY",
                                    entity_type="cross_reference",
                                    entity_id=t_id,
                                    message=f"Trip {t_id} rider_id '{trip_rider}' != payout line {line_id} rider_id '{r_id}'",
                                    details={
                                        "trip_id": t_id,
                                        "trip_rider_id": trip_rider,
                                        "payout_line_id": line_id,
                                        "payout_rider_id": r_id
                                    }
                                ))

                        payouts_by_trip.setdefault(t_id, []).append(row)

                        if line_type == "cancellation_penalty":
                            penalties_by_trip[t_id] = penalties_by_trip.get(t_id, 0) + 1
                            if penalties_by_trip[t_id] > 1:
                                report.issues.append(QualityIssue(
                                    category="duplicate_cancellation_penalty",
                                    severity="ANOMALY",
                                    entity_type="payout_line",
                                    entity_id=line_id,
                                    message=f"Duplicate cancellation penalty for trip '{t_id}'",
                                    details={"trip_id": t_id, "line_id": line_id, "count": penalties_by_trip[t_id]}
                                ))

        # 4. Check for completed trips without matching payout lines
        for t_id, trip_data in trips_by_id.items():
            if trip_data.get("status") == "completed" and t_id not in payouts_by_trip:
                report.issues.append(QualityIssue(
                    category="trip_missing_payout",
                    severity="WARNING",
                    entity_type="trip",
                    entity_id=t_id,
                    message=f"Completed trip {t_id} has no matching payout line in payout_lines.csv",
                    details={"trip_id": t_id, "rider_id": trip_data.get("rider_id")}
                ))

        return report

    async def load_all(self, session: AsyncSession) -> DataQualityReport:
        """Idempotently import CSV data into PostgreSQL/SQLite database and return quality report."""
        report = self.inspect_csv_data()
        self.report = report

        riders_file = self.data_dir / "riders.csv"
        trips_file = self.data_dir / "trips.csv"
        payout_file = self.data_dir / "payout_lines.csv"

        # 1. Load Riders
        if riders_file.exists():
            existing_riders = set((await session.execute(select(Rider.rider_id))).scalars().all())
            with open(riders_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                new_riders = []
                for row in reader:
                    r_id = row["rider_id"].strip()
                    if r_id in existing_riders:
                        continue
                    try:
                        rider = Rider(
                            rider_id=r_id,
                            name=row["name"].strip(),
                            city=row["city"].strip(),
                            joined_on=date.fromisoformat(row["joined_on"].strip()),
                        )
                        new_riders.append(rider)
                        existing_riders.add(r_id)
                    except Exception as e:
                        logger.error(f"Failed to parse rider row {row}: {e}")

                if new_riders:
                    session.add_all(new_riders)
                    await session.flush()
                    report.total_riders_imported = len(new_riders)
                    logger.info(f"Imported {len(new_riders)} new riders.")
                else:
                    report.total_riders_imported = 0
                    logger.info("Riders already loaded; skipped.")

        # 2. Load Trips
        if trips_file.exists():
            existing_trips = set((await session.execute(select(Trip.trip_id))).scalars().all())
            with open(trips_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                batch = []
                imported_count = 0
                for row in reader:
                    t_id = row["trip_id"].strip()
                    if t_id in existing_trips:
                        continue
                    try:
                        started_dt = datetime.fromisoformat(row["started_at"].strip().replace("Z", "+00:00"))
                        dist_val = Decimal(row["distance_km"].strip())
                        surge_val = Decimal(row["surge_multiplier"].strip())

                        trip = Trip(
                            trip_id=t_id,
                            rider_id=row["rider_id"].strip(),
                            started_at=started_dt,
                            status=row["status"].strip(),
                            distance_km=dist_val,
                            surge_multiplier=surge_val,
                        )
                        batch.append(trip)
                        existing_trips.add(t_id)

                        if len(batch) >= 500:
                            session.add_all(batch)
                            await session.flush()
                            imported_count += len(batch)
                            batch = []
                    except Exception as e:
                        logger.error(f"Failed to parse trip row {row}: {e}")

                if batch:
                    session.add_all(batch)
                    await session.flush()
                    imported_count += len(batch)

                report.total_trips_imported = imported_count
                logger.info(f"Imported {imported_count} new trips.")

        # 3. Load Payout Lines
        if payout_file.exists():
            existing_lines = set((await session.execute(select(PayoutLine.line_id))).scalars().all())
            with open(payout_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                batch = []
                imported_count = 0
                for row in reader:
                    l_id = row["line_id"].strip()
                    if l_id in existing_lines:
                        continue
                    try:
                        t_id = row["trip_id"].strip() if row.get("trip_id") and row["trip_id"].strip() else None
                        amt_val = Decimal(row["amount"].strip())

                        pl = PayoutLine(
                            line_id=l_id,
                            payout_date=date.fromisoformat(row["payout_date"].strip()),
                            rider_id=row["rider_id"].strip(),
                            line_type=row["line_type"].strip(),
                            trip_id=t_id,
                            amount=amt_val,
                        )
                        batch.append(pl)
                        existing_lines.add(l_id)

                        if len(batch) >= 500:
                            session.add_all(batch)
                            await session.flush()
                            imported_count += len(batch)
                            batch = []
                    except Exception as e:
                        logger.error(f"Failed to parse payout line {row}: {e}")

                if batch:
                    session.add_all(batch)
                    await session.flush()
                    imported_count += len(batch)

                report.total_payout_lines_imported = imported_count
                logger.info(f"Imported {imported_count} new payout lines.")

        await session.commit()
        return report


async def load_csv_data(session: AsyncSession) -> DataQualityReport:
    """Convenience function called on application startup or CLI."""
    loader = DataLoader()
    return await loader.load_all(session)
