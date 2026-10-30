"""Payout Reconciliation Service.

Reconciles rider payout disputes against:
1. Trip data (distance, surge, status)
2. Policy rules (fare formula, daily incentive, cancellation penalty)
3. Recorded payout lines in database/CSV
4. PaySwift provider records (to ensure no duplicate disbursements and verify status)

Produces a structured, fully auditable reconciliation result.
"""
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Rider, Trip, PayoutLine
from app.services.payout_calculator import (
    calculate_expected_payout_for_trip,
    calculate_daily_expected,
    calculate_trip_fare,
    to_decimal,
    to_ist_date,
    MAX_PLAUSIBLE_DISTANCE_KM,
    BASE_FARE,
    DAILY_INCENTIVE_AMOUNT,
    DAILY_INCENTIVE_TRIP_COUNT,
)
from app.services.payswift_client import PaySwiftClient

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    rider_id: str
    trip_id: Optional[str] = None
    dispute_date: Optional[str] = None
    category: str = "general"
    
    # Financial reconciliation amounts
    expected_amount: Decimal = Decimal("0")
    recorded_amount: Decimal = Decimal("0")
    payswift_paid_amount: Decimal = Decimal("0")
    verified_shortfall: Decimal = Decimal("0")  # Positive if rider is owed money
    overpayment_amount: Decimal = Decimal("0")   # Positive if rider was overpaid
    
    # Integrity and safety flags
    is_valid_identity: bool = True
    is_anomalous: bool = False
    anomaly_flags: List[str] = field(default_factory=list)
    auto_pay_eligible: bool = False
    
    # Audit and evidence
    evidence: Dict[str, Any] = field(default_factory=dict)
    audit_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Convert Decimals to float or str for JSON serialization
        for key in ["expected_amount", "recorded_amount", "payswift_paid_amount", "verified_shortfall", "overpayment_amount"]:
            if isinstance(data[key], Decimal):
                data[key] = str(data[key])
        return data


class ReconciliationService:
    def __init__(self, session: AsyncSession, payswift_client: Optional[PaySwiftClient] = None):
        self.session = session
        self.payswift_client = payswift_client

    async def reconcile_trip_dispute(
        self,
        rider_id: str,
        trip_id: str,
        skip_payswift: bool = False,
    ) -> ReconciliationResult:
        """Reconcile a dispute regarding a specific trip (e.g. missing surge, wrong fare, penalty)."""
        result = ReconciliationResult(rider_id=rider_id, trip_id=trip_id)

        # 1. Identify rider and validate identity
        rider_res = await self.session.execute(select(Rider).where(Rider.rider_id == rider_id))
        rider = rider_res.scalar_one_or_none()
        if not rider:
            result.is_valid_identity = False
            result.anomaly_flags.append(f"Rider '{rider_id}' not found in registered riders database")
            result.audit_notes.append(f"Rider identity validation failed for {rider_id}")

        # 2. Retrieve trip data
        trip_res = await self.session.execute(select(Trip).where(Trip.trip_id == trip_id))
        trip = trip_res.scalar_one_or_none()

        if not trip:
            result.anomaly_flags.append(f"Trip '{trip_id}' not found in database")
            result.audit_notes.append(f"Trip lookup failed for trip_id {trip_id}")
            result.is_anomalous = True
            result.auto_pay_eligible = False
            return result

        # Check for R7 vs R007 or mismatched rider ID
        if trip.rider_id != rider_id:
            result.is_valid_identity = False
            result.is_anomalous = True
            mismatch_msg = f"Rider ID mismatch: Trip {trip_id} belongs to rider '{trip.rider_id}', but dispute was filed for '{rider_id}'"
            result.anomaly_flags.append(mismatch_msg)
            result.audit_notes.append(mismatch_msg)
            # Never merge or normalize automatically!

        # 3. Check for distance and surge anomalies
        dist = to_decimal(trip.distance_km)
        surge = to_decimal(trip.surge_multiplier)

        if dist < Decimal("0"):
            result.is_anomalous = True
            result.anomaly_flags.append(f"Negative trip distance: {dist} km")
        elif dist > MAX_PLAUSIBLE_DISTANCE_KM:
            result.is_anomalous = True
            result.anomaly_flags.append(
                f"Anomalous distance {dist} km exceeds maximum plausible threshold ({MAX_PLAUSIBLE_DISTANCE_KM} km)"
            )

        if surge <= Decimal("0"):
            result.is_anomalous = True
            result.anomaly_flags.append(f"Invalid non-positive surge multiplier: {surge}")

        # 4. Calculate expected payout
        trip_dict = {
            "trip_id": trip.trip_id,
            "status": trip.status,
            "distance_km": dist,
            "surge_multiplier": surge,
        }
        calc_result = calculate_expected_payout_for_trip(trip_dict)
        expected_fare = calc_result["expected_amount"]
        result.expected_amount = expected_fare
        result.evidence["trip_calculation"] = calc_result

        # 5. Retrieve existing payout lines
        payout_res = await self.session.execute(
            select(PayoutLine).where(PayoutLine.trip_id == trip_id)
        )
        payout_lines = payout_res.scalars().all()

        recorded_total = Decimal("0")
        penalty_count = 0
        payout_line_details = []

        for line in payout_lines:
            amt = to_decimal(line.amount)
            recorded_total += amt
            payout_line_details.append({
                "line_id": line.line_id,
                "rider_id": line.rider_id,
                "line_type": line.line_type,
                "amount": str(amt),
                "payout_date": line.payout_date.isoformat(),
            })

            # Check if payout line rider matches trip rider
            if line.rider_id != trip.rider_id:
                mismatch_payout_msg = f"Payout line {line.line_id} rider '{line.rider_id}' differs from trip rider '{trip.rider_id}'"
                result.anomaly_flags.append(mismatch_payout_msg)
                result.is_anomalous = True

            if line.line_type == "cancellation_penalty":
                penalty_count += 1

        result.recorded_amount = recorded_total
        result.evidence["payout_lines"] = payout_line_details

        # Check for duplicate cancellation penalties (Scenario D)
        if penalty_count > 1:
            result.is_anomalous = True
            dup_pen_msg = f"Duplicate cancellation penalties detected: {penalty_count} penalty lines found for trip {trip_id}"
            result.anomaly_flags.append(dup_pen_msg)
            result.category = "duplicate_penalty"

        # 6. Check PaySwift records (if client available and not skipped)
        payswift_paid = Decimal("0")
        if self.payswift_client and not skip_payswift:
            try:
                payouts = await self.payswift_client.get_payouts(rider_id=rider_id)
                matched_payouts = []
                for p in payouts:
                    # Match by trip reference or dispute reference
                    ref = p.get("reference", "")
                    if trip_id in ref:
                        p_amt = Decimal(str(p.get("amount", 0)))
                        payswift_paid += p_amt
                        matched_payouts.append(p)
                result.payswift_paid_amount = payswift_paid
                result.evidence["payswift_matched_payouts"] = matched_payouts
            except Exception as e:
                logger.warning(f"PaySwift check during reconciliation failed: {e}")
                result.audit_notes.append(f"PaySwift query failed: {e}")

        # 7. Compare expected and actual amounts
        # Effective actual paid to rider = recorded in ledger + already disbursed in PaySwift
        effective_actual = recorded_total + payswift_paid

        # Calculate shortfall or overpayment
        if expected_fare > effective_actual:
            result.verified_shortfall = expected_fare - effective_actual
            result.overpayment_amount = Decimal("0")
        else:
            result.verified_shortfall = Decimal("0")
            result.overpayment_amount = effective_actual - expected_fare

        # Determine category if not set
        if trip.status == "cancelled_by_rider" and penalty_count > 1:
            result.category = "duplicate_penalty"
        elif surge > Decimal("1.0") and effective_actual < expected_fare:
            result.category = "surge_missing"
        elif len(payout_lines) == 0:
            result.category = "missing_payment"
        else:
            result.category = "fare_discrepancy"

        # 8. Check auto-pay eligibility
        # Safety rules:
        # - Identity must be strictly valid
        # - No anomalies (distance anomaly, R7 vs R007 mismatch, negative distance, etc.)
        # - Shortfall > 0 and <= ₹200
        is_safe_amount = Decimal("0") < result.verified_shortfall <= Decimal("200")
        result.auto_pay_eligible = (
            result.is_valid_identity
            and not result.is_anomalous
            and len(result.anomaly_flags) == 0
            and is_safe_amount
        )

        result.audit_notes.append(
            f"Reconciliation completed for trip {trip_id}. Expected: ₹{result.expected_amount}, "
            f"Recorded: ₹{result.recorded_amount}, Shortfall: ₹{result.verified_shortfall}, "
            f"Auto-pay eligible: {result.auto_pay_eligible}"
        )

        return result

    async def reconcile_daily_incentive_dispute(
        self,
        rider_id: str,
        dispute_date: date,
        skip_payswift: bool = False,
    ) -> ReconciliationResult:
        """Reconcile a dispute regarding daily incentive for a given IST calendar day."""
        result = ReconciliationResult(
            rider_id=rider_id,
            dispute_date=dispute_date.isoformat(),
            category="daily_incentive"
        )

        # 1. Identify rider
        rider_res = await self.session.execute(select(Rider).where(Rider.rider_id == rider_id))
        rider = rider_res.scalar_one_or_none()
        if not rider:
            result.is_valid_identity = False
            result.anomaly_flags.append(f"Rider '{rider_id}' not found in database")

        # 2. Retrieve all trips for this rider and count completed trips on this IST calendar day
        trips_res = await self.session.execute(
            select(Trip).where(Trip.rider_id == rider_id)
        )
        all_rider_trips = trips_res.scalars().all()

        day_trips = []
        for t in all_rider_trips:
            t_ist_date = to_ist_date(t.started_at)
            if t_ist_date == dispute_date:
                day_trips.append({
                    "trip_id": t.trip_id,
                    "status": t.status,
                    "distance_km": to_decimal(t.distance_km),
                    "surge_multiplier": to_decimal(t.surge_multiplier),
                })

        daily_calc = calculate_daily_expected(day_trips, dispute_date)
        result.evidence["daily_calculation"] = daily_calc

        expected_incentive = daily_calc["incentive_amount"]
        result.expected_amount = expected_incentive

        # 3. Retrieve recorded daily incentive payout lines for this date
        payout_res = await self.session.execute(
            select(PayoutLine).where(
                and_(
                    PayoutLine.rider_id == rider_id,
                    PayoutLine.payout_date == dispute_date,
                    PayoutLine.line_type == "daily_incentive"
                )
            )
        )
        incentive_lines = payout_res.scalars().all()

        recorded_incentive = Decimal("0")
        for line in incentive_lines:
            recorded_incentive += to_decimal(line.amount)

        if len(incentive_lines) > 1:
            result.is_anomalous = True
            result.anomaly_flags.append(
                f"Multiple daily incentive lines ({len(incentive_lines)}) found for rider {rider_id} on {dispute_date}"
            )

        result.recorded_amount = recorded_incentive

        # 4. Check PaySwift records
        payswift_paid = Decimal("0")
        if self.payswift_client and not skip_payswift:
            try:
                payouts = await self.payswift_client.get_payouts(rider_id=rider_id)
                for p in payouts:
                    ref = p.get("reference", "")
                    if f"incentive-{dispute_date.isoformat()}" in ref:
                        payswift_paid += Decimal(str(p.get("amount", 0)))
                result.payswift_paid_amount = payswift_paid
            except Exception as e:
                logger.warning(f"PaySwift query failed during incentive reconciliation: {e}")

        # 5. Shortfall calculation
        effective_actual = recorded_incentive + payswift_paid
        if expected_incentive > effective_actual:
            result.verified_shortfall = expected_incentive - effective_actual
            result.overpayment_amount = Decimal("0")
        else:
            result.verified_shortfall = Decimal("0")
            result.overpayment_amount = effective_actual - expected_incentive

        # 6. Auto-pay eligibility check
        is_safe_amount = Decimal("0") < result.verified_shortfall <= Decimal("200")
        result.auto_pay_eligible = (
            result.is_valid_identity
            and not result.is_anomalous
            and len(result.anomaly_flags) == 0
            and is_safe_amount
        )

        return result
