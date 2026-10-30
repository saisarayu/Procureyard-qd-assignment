"""Unit tests for the deterministic payout calculator."""
from datetime import datetime, date, timezone
from decimal import Decimal
import pytest

from app.services.payout_calculator import (
    calculate_trip_fare,
    calculate_cancellation_penalty,
    calculate_daily_incentive,
    calculate_expected_payout_for_trip,
    calculate_daily_expected,
    compute_shortfall,
    to_ist_date,
    is_within_dispute_window,
    DistanceAnomalyError,
    InvalidSurgeError,
    BASE_FARE,
    DAILY_INCENTIVE_AMOUNT,
)


class TestFareCalculation:
    def test_distance_below_2km(self):
        """Trips under 2 km pay the base fare of ₹25."""
        assert calculate_trip_fare(Decimal("0.5")) == Decimal("25")
        assert calculate_trip_fare(Decimal("1.0")) == Decimal("25")
        assert calculate_trip_fare(Decimal("1.8")) == Decimal("25")

    def test_distance_exactly_2km(self):
        """Trip of exactly 2 km pays the base fare of ₹25."""
        assert calculate_trip_fare(Decimal("2.0")) == Decimal("25")

    def test_distance_above_2km_no_surge(self):
        """Extra km over 2 km is charged at ₹6/km."""
        # 3 km -> 25 + 1 * 6 = 31
        assert calculate_trip_fare(Decimal("3.0")) == Decimal("31")
        # 6 km -> 25 + 4 * 6 = 49
        assert calculate_trip_fare(Decimal("6.0")) == Decimal("49")
        # 10 km -> 25 + 8 * 6 = 73
        assert calculate_trip_fare(Decimal("10.0")) == Decimal("73")

    def test_scenario_a_surge_and_half_rupee_rounding(self):
        """Scenario A: R003, T926334. Distance 6 km, surge 1.5.
        Base + Distance: 25 + 4 * 6 = 49.
        Surge: 49 * 1.5 = 73.5.
        Half-rupee rounding: 73.5 rounds UP to 74.
        """
        fare = calculate_trip_fare(distance_km=Decimal("6.0"), surge_multiplier=Decimal("1.5"))
        assert fare == Decimal("74")

    def test_scenario_b_surge(self):
        """Scenario B: R027, T604546. Distance 10 km, surge 1.2.
        Base + Distance: 25 + 8 * 6 = 73.
        Surge: 73 * 1.2 = 87.6.
        Rounded: 88.
        """
        fare = calculate_trip_fare(distance_km=Decimal("10.0"), surge_multiplier=Decimal("1.2"))
        assert fare == Decimal("88")

    def test_half_rupee_rounding_half_up(self):
        """0.5 always rounds up to the nearest rupee."""
        # (25 + 0) * 1.1 = 27.5 -> 28
        fare = calculate_trip_fare(distance_km=Decimal("2.0"), surge_multiplier=Decimal("1.1"))
        assert fare == Decimal("28")

    def test_negative_distance_raises_error(self):
        """Negative distance must raise DistanceAnomalyError."""
        with pytest.raises(DistanceAnomalyError):
            calculate_trip_fare(distance_km=Decimal("-1.5"))

    def test_zero_or_negative_surge_raises_error(self):
        """Zero or negative surge must raise InvalidSurgeError."""
        with pytest.raises(InvalidSurgeError):
            calculate_trip_fare(distance_km=Decimal("5.0"), surge_multiplier=Decimal("0.0"))
        with pytest.raises(InvalidSurgeError):
            calculate_trip_fare(distance_km=Decimal("5.0"), surge_multiplier=Decimal("-1.2"))

    def test_scenario_f_anomalous_distance(self):
        """Distance exceeding 100 km (e.g. 5000 km) raises DistanceAnomalyError."""
        with pytest.raises(DistanceAnomalyError):
            calculate_trip_fare(distance_km=Decimal("5000.0"))


class TestTripStatusAndPenalties:
    def test_customer_cancellation(self):
        """Customer cancellation earns nothing and incurs no penalty."""
        trip = {
            "trip_id": "T_CUST_CANCEL",
            "status": "cancelled_by_customer",
            "distance_km": Decimal("5.0"),
            "surge_multiplier": Decimal("1.0"),
        }
        res = calculate_expected_payout_for_trip(trip)
        assert res["expected_amount"] == Decimal("0")
        assert not res["is_anomalous"]

    def test_rider_cancellation(self):
        """Rider cancellation incurs a ₹10 penalty (-10)."""
        trip = {
            "trip_id": "T_RIDER_CANCEL",
            "status": "cancelled_by_rider",
            "distance_km": Decimal("5.0"),
            "surge_multiplier": Decimal("1.0"),
        }
        res = calculate_expected_payout_for_trip(trip)
        assert res["expected_amount"] == Decimal("-10")
        assert calculate_cancellation_penalty() == Decimal("-10")

    def test_anomalous_trip_flagged_in_calculator(self):
        """Trip with 5000 km is flagged as anomalous with zero expected payout."""
        trip = {
            "trip_id": "T312538",
            "status": "completed",
            "distance_km": Decimal("5000.0"),
            "surge_multiplier": Decimal("1.0"),
        }
        res = calculate_expected_payout_for_trip(trip)
        assert res["is_anomalous"] is True
        assert res["expected_amount"] == Decimal("0")
        assert "exceeds plausible threshold" in res["anomaly_reason"]


class TestDailyIncentive:
    def test_11_completed_trips_no_incentive(self):
        """11 completed trips does not qualify for the ₹150 incentive."""
        assert calculate_daily_incentive(11) == Decimal("0")

    def test_12_completed_trips_qualifies(self):
        """12 completed trips qualifies for the ₹150 incentive."""
        assert calculate_daily_incentive(12) == Decimal("150")

    def test_scenario_c_13_completed_trips(self):
        """Scenario C: R005 with 13 completed trips qualifies for ₹150 incentive."""
        assert calculate_daily_incentive(13) == Decimal("150")


class TestTimezoneAndDisputeWindow:
    def test_ist_midnight_boundary(self):
        """2026-09-20 18:30:00 UTC is exactly 2026-09-21 00:00:00 in IST."""
        utc_dt = datetime(2026, 9, 20, 18, 30, 0, tzinfo=timezone.utc)
        ist_date = to_ist_date(utc_dt)
        assert ist_date == date(2026, 9, 21)

    def test_ist_before_midnight_boundary(self):
        """2026-09-20 18:29:59 UTC is 2026-09-20 in IST."""
        utc_dt = datetime(2026, 9, 20, 18, 29, 59, tzinfo=timezone.utc)
        ist_date = to_ist_date(utc_dt)
        assert ist_date == date(2026, 9, 20)

    def test_dispute_window_7_days(self):
        """Only disputes within 7 days of reference date are allowed."""
        ref = date(2026, 9, 21)
        assert is_within_dispute_window(date(2026, 9, 21), ref) is True
        assert is_within_dispute_window(date(2026, 9, 14), ref) is True  # Exactly 7 days
        assert is_within_dispute_window(date(2026, 9, 13), ref) is False # 8 days ago


class TestShortfallCalculation:
    def test_positive_shortfall(self):
        """When expected > actual_paid, shortfall is positive."""
        assert compute_shortfall(Decimal("74"), Decimal("49")) == Decimal("25")

    def test_no_shortfall_when_overpaid(self):
        """When actual_paid >= expected, shortfall is 0."""
        assert compute_shortfall(Decimal("49"), Decimal("74")) == Decimal("0")
        assert compute_shortfall(Decimal("50"), Decimal("50")) == Decimal("0")
