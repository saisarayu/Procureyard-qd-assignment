"""Deterministic payout calculator implementing the official QuickDrop policy.

Policy rules:
- Base fare: ₹25 per completed trip.
- Distance: ₹6 per kilometre after the first 2 km.
- Formula: fare = 25 + max(0, distance_km - 2) * 6
- Surge: Apply surge_multiplier to the entire trip fare (base + distance).
- Rounding: Round each trip to the nearest rupee using ROUND_HALF_UP. Exactly 0.5 rounds up.
- Daily incentive: ₹150 for 12 or more completed trips per rider per IST calendar day.
- Cancellation penalty: ₹10 per rider-cancelled trip.
- Customer cancellation: No payout and no penalty.
- Timezone: Asia/Kolkata (IST = UTC+5:30).
- Dispute window: Last seven days, following docs/policy.md.

All financial calculations use Python's Decimal class. Floating-point arithmetic is never used for money.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import logging
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Constants
IST = timezone(timedelta(hours=5, minutes=30))
BASE_FARE = Decimal("25")
RATE_PER_KM = Decimal("6")
BASE_DISTANCE_KM = Decimal("2")
DAILY_INCENTIVE_AMOUNT = Decimal("150")
DAILY_INCENTIVE_TRIP_COUNT = 12
RIDER_CANCELLATION_PENALTY = Decimal("-10")
CUSTOMER_CANCELLATION_PAYOUT = Decimal("0")
MAX_PLAUSIBLE_DISTANCE_KM = Decimal("100.0")  # Flag anything above as anomalous (e.g., 5000 km)
DISPUTE_WINDOW_DAYS = 7


class PayoutCalculationError(Exception):
    """Base exception for calculation errors."""
    pass


class DistanceAnomalyError(PayoutCalculationError):
    """Raised when distance is anomalous or impossible (e.g. 5000 km or negative)."""
    pass


class InvalidSurgeError(PayoutCalculationError):
    """Raised when surge multiplier is invalid (<= 0)."""
    pass


def to_decimal(val: Union[int, float, str, Decimal]) -> Decimal:
    """Safely convert any numeric value to Decimal without float imprecision."""
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val))


def to_ist_date(dt: Union[datetime, str]) -> date:
    """Convert a UTC or aware/naive datetime to an IST calendar date.
    
    If dt is a string, it parses ISO format.
    If dt has no tzinfo, it is assumed to be UTC.
    """
    if isinstance(dt, str):
        # Handle ISO format with or without 'Z'
        clean_str = dt.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(IST)
    return ist_dt.date()


def is_within_dispute_window(
    target_date: Union[date, datetime],
    reference_date: Optional[Union[date, datetime]] = None,
    window_days: int = DISPUTE_WINDOW_DAYS,
) -> bool:
    """Check if target_date falls within the last `window_days` days from reference_date.
    
    If reference_date is None, current date in IST is used.
    """
    if isinstance(target_date, datetime):
        t_date = to_ist_date(target_date)
    else:
        t_date = target_date

    if reference_date is None:
        ref_date = datetime.now(IST).date()
    elif isinstance(reference_date, datetime):
        ref_date = to_ist_date(reference_date)
    else:
        ref_date = reference_date

    delta = (ref_date - t_date).days
    return 0 <= delta <= window_days


def calculate_trip_fare(
    distance_km: Union[int, float, str, Decimal],
    surge_multiplier: Union[int, float, str, Decimal] = Decimal("1.0"),
    allow_anomaly: bool = False,
) -> Decimal:
    """Calculate the correct fare for a single completed trip using Decimal arithmetic.

    Formula:
        fare = (25 + max(0, distance_km - 2) * 6) * surge_multiplier
        rounded to nearest rupee using ROUND_HALF_UP (0.5 rounds up).

    Raises:
        DistanceAnomalyError: if distance < 0 or > MAX_PLAUSIBLE_DISTANCE_KM (unless allow_anomaly=True)
        InvalidSurgeError: if surge_multiplier <= 0
    """
    dist = to_decimal(distance_km)
    surge = to_decimal(surge_multiplier)

    if dist < Decimal("0"):
        raise DistanceAnomalyError(f"Distance cannot be negative: {dist} km")

    if dist > MAX_PLAUSIBLE_DISTANCE_KM and not allow_anomaly:
        raise DistanceAnomalyError(
            f"Distance {dist} km exceeds maximum plausible threshold ({MAX_PLAUSIBLE_DISTANCE_KM} km)"
        )

    if surge <= Decimal("0"):
        raise InvalidSurgeError(f"Surge multiplier must be positive: {surge}")

    extra_km = max(Decimal("0"), dist - BASE_DISTANCE_KM)
    distance_fare = extra_km * RATE_PER_KM
    base_and_distance = BASE_FARE + distance_fare
    raw_fare = base_and_distance * surge

    # Round to nearest rupee with ROUND_HALF_UP
    return raw_fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def calculate_cancellation_penalty() -> Decimal:
    """Return the penalty for a rider-cancelled trip: -₹10."""
    return RIDER_CANCELLATION_PENALTY


def calculate_daily_incentive(completed_trip_count: int) -> Decimal:
    """Calculate daily incentive: ₹150 if 12 or more completed trips, else 0."""
    if completed_trip_count >= DAILY_INCENTIVE_TRIP_COUNT:
        return DAILY_INCENTIVE_AMOUNT
    return Decimal("0")


def calculate_expected_payout_for_trip(trip: Dict) -> Dict:
    """Calculate expected payout for a single trip record.

    Args:
        trip: dict with keys: trip_id, status, distance_km, surge_multiplier

    Returns:
        dict with:
            - trip_id
            - status
            - expected_amount (Decimal)
            - is_anomalous (bool)
            - anomaly_reason (str or None)
            - breakdown (dict)
    """
    trip_id = trip.get("trip_id")
    status = trip.get("status", "")
    dist_val = trip.get("distance_km", Decimal("0"))
    surge_val = trip.get("surge_multiplier", Decimal("1.0"))

    try:
        dist = to_decimal(dist_val)
    except (InvalidOperation, TypeError, ValueError):
        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": Decimal("0"),
            "is_anomalous": True,
            "anomaly_reason": f"Invalid distance value: {dist_val}",
            "breakdown": {},
        }

    try:
        surge = to_decimal(surge_val)
    except (InvalidOperation, TypeError, ValueError):
        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": Decimal("0"),
            "is_anomalous": True,
            "anomaly_reason": f"Invalid surge multiplier value: {surge_val}",
            "breakdown": {},
        }

    if status == "completed":
        is_anomalous = False
        anomaly_reason = None

        if dist < Decimal("0"):
            is_anomalous = True
            anomaly_reason = f"Negative distance: {dist} km"
        elif dist > MAX_PLAUSIBLE_DISTANCE_KM:
            is_anomalous = True
            anomaly_reason = f"Distance {dist} km exceeds plausible threshold {MAX_PLAUSIBLE_DISTANCE_KM} km"

        if surge <= Decimal("0"):
            is_anomalous = True
            anomaly_reason = f"Surge multiplier must be positive: {surge}"

        if is_anomalous:
            return {
                "trip_id": trip_id,
                "status": status,
                "expected_amount": Decimal("0"),
                "is_anomalous": True,
                "anomaly_reason": anomaly_reason,
                "breakdown": {
                    "distance_km": dist,
                    "surge_multiplier": surge,
                },
            }

        extra_km = max(Decimal("0"), dist - BASE_DISTANCE_KM)
        distance_fare = extra_km * RATE_PER_KM
        base_plus_distance = BASE_FARE + distance_fare
        raw_fare = base_plus_distance * surge
        rounded_fare = raw_fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": rounded_fare,
            "is_anomalous": False,
            "anomaly_reason": None,
            "breakdown": {
                "base": BASE_FARE,
                "distance_km": dist,
                "extra_km": extra_km,
                "distance_fare": distance_fare,
                "surge_multiplier": surge,
                "raw_fare": raw_fare,
                "rounded_fare": rounded_fare,
            },
        }

    elif status == "cancelled_by_rider":
        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": RIDER_CANCELLATION_PENALTY,
            "is_anomalous": False,
            "anomaly_reason": None,
            "breakdown": {"penalty": RIDER_CANCELLATION_PENALTY},
        }

    elif status == "cancelled_by_customer":
        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": CUSTOMER_CANCELLATION_PAYOUT,
            "is_anomalous": False,
            "anomaly_reason": None,
            "breakdown": {"note": "Customer cancellation earns nothing and incurs no penalty"},
        }

    else:
        return {
            "trip_id": trip_id,
            "status": status,
            "expected_amount": Decimal("0"),
            "is_anomalous": True,
            "anomaly_reason": f"Unknown trip status: {status}",
            "breakdown": {},
        }


def calculate_daily_expected(trips: List[Dict], payout_date: date) -> Dict:
    """Calculate the expected payout for a rider for an entire IST calendar day.

    Args:
        trips: list of trip dicts belonging to the rider on that IST day.
        payout_date: the IST calendar date.

    Returns:
        dict with detailed calculations, incentive status, and total expected amount.
    """
    completed_trips = []
    cancelled_by_rider = []
    cancelled_by_customer = []
    trip_calculations = []
    total_expected = Decimal("0")
    has_anomalies = False

    for trip in trips:
        calc = calculate_expected_payout_for_trip(trip)
        trip_calculations.append(calc)

        if calc["is_anomalous"]:
            has_anomalies = True
        else:
            total_expected += calc["expected_amount"]

        status = trip.get("status", "")
        if status == "completed":
            completed_trips.append(trip)
        elif status == "cancelled_by_rider":
            cancelled_by_rider.append(trip)
        elif status == "cancelled_by_customer":
            cancelled_by_customer.append(trip)

    # Daily incentive: ₹150 if 12 or more completed trips
    incentive_eligible = len(completed_trips) >= DAILY_INCENTIVE_TRIP_COUNT
    incentive_amount = DAILY_INCENTIVE_AMOUNT if incentive_eligible else Decimal("0")
    total_expected += incentive_amount

    return {
        "date": payout_date.isoformat(),
        "completed_count": len(completed_trips),
        "cancelled_by_rider_count": len(cancelled_by_rider),
        "cancelled_by_customer_count": len(cancelled_by_customer),
        "trip_calculations": trip_calculations,
        "incentive_eligible": incentive_eligible,
        "incentive_amount": incentive_amount,
        "total_expected": total_expected,
        "has_anomalies": has_anomalies,
    }


def compute_shortfall(expected: Decimal, actual_paid: Decimal) -> Decimal:
    """Compute the shortfall (how much extra the rider should receive).

    Returns:
        expected - actual_paid if positive, else Decimal("0").
    """
    diff = expected - actual_paid
    return max(Decimal("0"), diff)
