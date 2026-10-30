"""Agent tools for QuickDrop Rider Payout Dispute Desk.

All financial calculations, validation, and execution are deterministic Python code.
LLM provides natural language understanding and explanation, but cannot override tools.
"""
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
import logging
from typing import Dict, List, Optional, Any
from uuid import UUID, uuid4
from pydantic import BaseModel, Field

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.database import (
    Rider, Trip, PayoutLine, Dispute, PaymentAttempt, ApprovalRequest,
    DisputeStatus, PaymentStatus, ApprovalStatus, OpsItemType
)
from app.services.payout_calculator import (
    calculate_trip_fare, calculate_daily_incentive, calculate_cancellation_penalty,
    compute_shortfall, to_ist_date, is_within_dispute_window, MAX_PLAUSIBLE_DISTANCE_KM, IST
)
from app.services.reconciliation_service import ReconciliationService, ReconciliationResult
from app.services.payswift_client import PaySwiftClient, PaySwiftError, PaySwiftUnavailable, PaySwiftTimeout

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Tool Input / Output Schemas ──────────────────────────────────────

class IdentifyRiderResult(BaseModel):
    rider_id: str
    exists: bool
    name: Optional[str] = None
    city: Optional[str] = None
    joined_on: Optional[str] = None


class DisputeExtractionResult(BaseModel):
    intent: str
    trip_id: Optional[str] = None
    disputed_date: Optional[str] = None
    claimed_amount: Optional[Decimal] = None
    description: str
    confidence: float = 1.0
    needs_clarification: bool = False
    clarification_question: Optional[str] = None


class TripRecord(BaseModel):
    trip_id: str
    rider_id: str
    started_at: str
    status: str
    distance_km: Decimal
    surge_multiplier: Decimal


class PayoutRecord(BaseModel):
    line_id: str
    payout_date: str
    rider_id: str
    line_type: str
    trip_id: Optional[str] = None
    amount: Decimal


class ExpectedPayoutResult(BaseModel):
    item_type: str  # "trip" or "incentive" or "penalty"
    expected_amount: Decimal
    details: Dict[str, Any] = {}


class DisputeWindowResult(BaseModel):
    is_valid: bool
    dispute_date: str
    reference_date: str
    days_difference: int
    allowed_window_days: int = 7


class AutoPayEligibilityResult(BaseModel):
    eligible: bool
    reason: str
    amount: Decimal
    daily_count: int
    max_amount_limit: Decimal
    max_daily_limit: int


class PreviousDisputesResult(BaseModel):
    has_previous: bool
    previous_count: int
    disputes: List[Dict[str, Any]] = []


class ApprovalRequestResult(BaseModel):
    approval_id: str
    dispute_id: str
    rider_id: str
    type: str
    amount: Optional[Decimal] = None
    reason: str
    status: str


class PaymentExecutionResult(BaseModel):
    success: bool
    payment_id: Optional[str] = None
    payswift_payout_id: Optional[str] = None
    status: str
    amount: Decimal
    error_message: Optional[str] = None


class PaymentVerificationResult(BaseModel):
    verified: bool
    payout_id: Optional[str] = None
    status: Optional[str] = None
    provider_record: Optional[Dict[str, Any]] = None


# ── Deterministic Agent Tools Implementation ─────────────────────────

class AgentTools:
    def __init__(self, session: AsyncSession, payswift_client: Optional[PaySwiftClient] = None):
        self.session = session
        self.payswift_client = payswift_client or PaySwiftClient()
        self.reconciliation_service = ReconciliationService(session, self.payswift_client)

    async def identify_rider(self, rider_id: str) -> IdentifyRiderResult:
        """Tool 1: Verify whether rider exists in QuickDrop records."""
        result = await self.session.execute(select(Rider).where(Rider.rider_id == rider_id))
        rider = result.scalar_one_or_none()
        if not rider:
            return IdentifyRiderResult(rider_id=rider_id, exists=False)
        return IdentifyRiderResult(
            rider_id=rider.rider_id,
            exists=True,
            name=rider.name,
            city=rider.city,
            joined_on=rider.joined_on.isoformat(),
        )

    def check_dispute_window(self, dispute_date: date, received_at: datetime) -> DisputeWindowResult:
        """Tool 7: Check whether the disputed date falls within the 7-day policy window."""
        ref_date = to_ist_date(received_at)
        days_diff = (ref_date - dispute_date).days
        valid = is_within_dispute_window(dispute_date, received_at, window_days=settings.DISPUTE_WINDOW_DAYS)
        return DisputeWindowResult(
            is_valid=valid,
            dispute_date=dispute_date.isoformat(),
            reference_date=ref_date.isoformat(),
            days_difference=days_diff,
            allowed_window_days=settings.DISPUTE_WINDOW_DAYS,
        )

    async def retrieve_trips(
        self,
        rider_id: str,
        trip_id: Optional[str] = None,
        dispute_date: Optional[date] = None,
    ) -> List[TripRecord]:
        """Tool 3: Retrieve actual trip records for a rider from the database."""
        conditions = [Trip.rider_id == rider_id]
        if trip_id:
            # Query trip directly by ID (allows checking ownership)
            result = await self.session.execute(select(Trip).where(Trip.trip_id == trip_id))
            trips = result.scalars().all()
        elif dispute_date:
            ist_start = datetime(dispute_date.year, dispute_date.month, dispute_date.day, 0, 0, 0, tzinfo=IST)
            ist_end = ist_start + timedelta(days=1)
            utc_start = ist_start.astimezone(timezone.utc)
            utc_end = ist_end.astimezone(timezone.utc)
            result = await self.session.execute(
                select(Trip).where(
                    and_(
                        Trip.rider_id == rider_id,
                        Trip.started_at >= utc_start,
                        Trip.started_at < utc_end,
                    )
                ).order_by(Trip.started_at)
            )
            trips = result.scalars().all()
        else:
            result = await self.session.execute(
                select(Trip).where(Trip.rider_id == rider_id).order_by(Trip.started_at.desc()).limit(20)
            )
            trips = result.scalars().all()

        return [
            TripRecord(
                trip_id=t.trip_id,
                rider_id=t.rider_id,
                started_at=t.started_at.isoformat(),
                status=t.status,
                distance_km=Decimal(str(t.distance_km)),
                surge_multiplier=Decimal(str(t.surge_multiplier)),
            )
            for t in trips
        ]

    async def retrieve_payouts(
        self,
        rider_id: str,
        trip_id: Optional[str] = None,
        payout_date: Optional[date] = None,
    ) -> List[PayoutRecord]:
        """Tool 4: Retrieve payout line records from database."""
        conditions = []
        if trip_id:
            conditions.append(PayoutLine.trip_id == trip_id)
        else:
            conditions.append(PayoutLine.rider_id == rider_id)
            if payout_date:
                conditions.append(PayoutLine.payout_date == payout_date)

        result = await self.session.execute(
            select(PayoutLine).where(and_(*conditions)).order_by(PayoutLine.created_at.desc())
        )
        lines = result.scalars().all()
        return [
            PayoutRecord(
                line_id=pl.line_id,
                payout_date=pl.payout_date.isoformat(),
                rider_id=pl.rider_id,
                line_type=pl.line_type,
                trip_id=pl.trip_id,
                amount=Decimal(str(pl.amount)),
            )
            for pl in lines
        ]

    def calculate_expected_payout(
        self,
        distance_km: Optional[Decimal] = None,
        surge_multiplier: Optional[Decimal] = None,
        completed_trips_count: Optional[int] = None,
        is_rider_cancellation: bool = False,
    ) -> ExpectedPayoutResult:
        """Tool 5: Deterministic payout calculation following official policy rules."""
        if completed_trips_count is not None:
            incentive = calculate_daily_incentive(completed_trips_count)
            return ExpectedPayoutResult(
                item_type="incentive",
                expected_amount=incentive,
                details={"completed_trips": completed_trips_count, "qualifies": completed_trips_count >= 12},
            )
        elif is_rider_cancellation:
            penalty = calculate_cancellation_penalty()
            return ExpectedPayoutResult(
                item_type="penalty",
                expected_amount=penalty,
                details={"penalty": str(penalty)},
            )
        elif distance_km is not None and surge_multiplier is not None:
            fare = calculate_trip_fare(distance_km, surge_multiplier)
            return ExpectedPayoutResult(
                item_type="trip",
                expected_amount=fare,
                details={"distance_km": str(distance_km), "surge_multiplier": str(surge_multiplier)},
            )
        else:
            raise ValueError("Insufficient arguments for calculate_expected_payout")

    async def reconcile_payout(
        self,
        rider_id: str,
        trip_id: Optional[str] = None,
        dispute_date: Optional[date] = None,
        skip_payswift: bool = False,
    ) -> ReconciliationResult:
        """Tool 6: Run multi-layer reconciliation against database, CSV records, and PaySwift."""
        if trip_id:
            return await self.reconciliation_service.reconcile_trip_dispute(
                rider_id=rider_id,
                trip_id=trip_id,
                skip_payswift=skip_payswift,
            )
        elif dispute_date:
            return await self.reconciliation_service.reconcile_daily_incentive_dispute(
                rider_id=rider_id,
                dispute_date=dispute_date,
                skip_payswift=skip_payswift,
            )
        else:
            raise ValueError("Either trip_id or dispute_date is required for reconciliation")

    async def check_previous_disputes(self, rider_id: str, trip_id: Optional[str] = None) -> PreviousDisputesResult:
        """Tool 9: Check whether rider has previous disputes on this trip or in general."""
        conditions = [Dispute.rider_id == rider_id]
        if trip_id:
            conditions.append(Dispute.trip_id == trip_id)

        result = await self.session.execute(
            select(Dispute).where(and_(*conditions)).order_by(Dispute.created_at.desc())
        )
        disputes = result.scalars().all()
        return PreviousDisputesResult(
            has_previous=len(disputes) > 0,
            previous_count=len(disputes),
            disputes=[
                {
                    "id": str(d.id),
                    "category": d.category,
                    "status": d.status,
                    "shortfall": str(d.shortfall) if d.shortfall else "0",
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in disputes
            ],
        )

    async def check_auto_pay_eligibility(
        self,
        rider_id: str,
        amount: Decimal,
        trip_id: Optional[str] = None,
        is_anomalous: bool = False,
        is_valid_identity: bool = True,
        is_repeat: bool = False,
    ) -> AutoPayEligibilityResult:
        """Tool 8: Enforce financial safety limits for automatic resolution."""
        max_amount = Decimal(str(settings.AUTO_PAY_MAX_AMOUNT))  # ₹200

        # Safety checks
        if not is_valid_identity:
            return AutoPayEligibilityResult(
                eligible=False,
                reason="Rider identity mismatch or unverified account. Requires human review.",
                amount=amount,
                daily_count=0,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        if is_anomalous:
            return AutoPayEligibilityResult(
                eligible=False,
                reason="Data anomaly detected (e.g. extreme distance, duplicate deductions). Requires human review.",
                amount=amount,
                daily_count=0,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        if is_repeat:
            return AutoPayEligibilityResult(
                eligible=False,
                reason="Repeat dispute for same trip/date. Requires human review.",
                amount=amount,
                daily_count=0,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        if amount <= Decimal("0"):
            return AutoPayEligibilityResult(
                eligible=False,
                reason="No positive shortfall owed to rider.",
                amount=amount,
                daily_count=0,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        if amount > max_amount:
            return AutoPayEligibilityResult(
                eligible=False,
                reason=f"Dispute amount ₹{amount} exceeds auto-pay policy limit of ₹{max_amount}.",
                amount=amount,
                daily_count=0,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        # Serialize eligibility checks for one rider until the surrounding transaction commits.
        await self.session.execute(
            select(Rider.rider_id)
            .where(Rider.rider_id == rider_id)
            .with_for_update()
        )

        # Count auto-payments made to this rider today (UTC)
        today = datetime.now(timezone.utc).date()
        count_q = await self.session.execute(
            select(func.count()).select_from(PaymentAttempt).where(
                and_(
                    PaymentAttempt.rider_id == rider_id,
                    PaymentAttempt.status == PaymentStatus.PAID.value,
                    func.date(PaymentAttempt.created_at) == today,
                )
            )
        )
        daily_count = count_q.scalar() or 0

        if daily_count >= settings.AUTO_PAY_MAX_PER_DAY:
            return AutoPayEligibilityResult(
                eligible=False,
                reason=f"Daily auto-pay limit of {settings.AUTO_PAY_MAX_PER_DAY} payment per day reached for rider.",
                amount=amount,
                daily_count=daily_count,
                max_amount_limit=max_amount,
                max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
            )

        return AutoPayEligibilityResult(
            eligible=True,
            reason="Within ₹200 limit and 1 auto-pay per day policy.",
            amount=amount,
            daily_count=daily_count,
            max_amount_limit=max_amount,
            max_daily_limit=settings.AUTO_PAY_MAX_PER_DAY,
        )

    async def create_approval_request(
        self,
        dispute_id: UUID,
        rider_id: str,
        amount: Optional[Decimal],
        reason: str,
        evidence_summary: Optional[str] = None,
    ) -> ApprovalRequestResult:
        """Tool 10: Create approval request for operations team review."""
        approval = ApprovalRequest(
            id=uuid4(),
            dispute_id=dispute_id,
            rider_id=rider_id,
            type=OpsItemType.APPROVAL.value,
            amount=amount,
            reason=reason,
            evidence_summary=evidence_summary,
            status=ApprovalStatus.PENDING.value,
        )
        self.session.add(approval)
        await self.session.flush()

        return ApprovalRequestResult(
            approval_id=str(approval.id),
            dispute_id=str(dispute_id),
            rider_id=rider_id,
            type=OpsItemType.APPROVAL.value,
            amount=amount,
            reason=reason,
            status=ApprovalStatus.PENDING.value,
        )

    async def create_escalation(
        self,
        dispute_id: UUID,
        rider_id: str,
        reason: str,
        evidence_summary: Optional[str] = None,
    ) -> ApprovalRequestResult:
        """Tool 11: Create escalation for suspicious or anomalous dispute."""
        escalation = ApprovalRequest(
            id=uuid4(),
            dispute_id=dispute_id,
            rider_id=rider_id,
            type=OpsItemType.ESCALATION.value,
            amount=None,
            reason=reason,
            evidence_summary=evidence_summary,
            status=ApprovalStatus.PENDING.value,
        )
        self.session.add(escalation)
        await self.session.flush()

        return ApprovalRequestResult(
            approval_id=str(escalation.id),
            dispute_id=str(dispute_id),
            rider_id=rider_id,
            type=OpsItemType.ESCALATION.value,
            amount=None,
            reason=reason,
            status=ApprovalStatus.PENDING.value,
        )

    async def execute_payswift_payment(
        self,
        dispute_id: UUID,
        rider_id: str,
        amount: Decimal,
        reference: str,
        idempotency_key: str,
    ) -> PaymentExecutionResult:
        """Tool 12: Execute financial payment through PaySwift sandbox."""
        # 1. Create database PaymentAttempt record first (status = PROCESSING)
        payment_attempt = PaymentAttempt(
            id=uuid4(),
            dispute_id=dispute_id,
            rider_id=rider_id,
            amount=amount,
            idempotency_key=idempotency_key,
            status=PaymentStatus.PROCESSING.value,
            reference=reference,
        )
        self.session.add(payment_attempt)
        await self.session.flush()

        int_amount = int(amount)

        try:
            resp = await self.payswift_client.create_payout(
                rider_id=rider_id,
                amount=int_amount,
                reference=reference,
                idempotency_key=idempotency_key,
            )

            payout_id = resp.get("payout_id")
            ps_status = resp.get("status", "paid")

            payment_attempt.payswift_payout_id = payout_id
            payment_attempt.payswift_status = ps_status
            payment_attempt.status = PaymentStatus.PAID.value
            payment_attempt.verified_at = datetime.now(timezone.utc)
            await self.session.flush()

            return PaymentExecutionResult(
                success=True,
                payment_id=str(payment_attempt.id),
                payswift_payout_id=payout_id,
                status=PaymentStatus.PAID.value,
                amount=amount,
            )

        except PaySwiftTimeout as e:
            logger.warning(f"PaySwift timeout during payment: {e}")
            payment_attempt.status = PaymentStatus.VERIFICATION_REQUIRED.value
            payment_attempt.error_message = f"Timeout: {e}"
            await self.session.flush()
            return PaymentExecutionResult(
                success=False,
                payment_id=str(payment_attempt.id),
                status=PaymentStatus.VERIFICATION_REQUIRED.value,
                amount=amount,
                error_message=f"PaySwift timeout - transaction marked for verification: {e}",
            )

        except PaySwiftUnavailable as e:
            logger.error(f"PaySwift unavailable: {e}")
            payment_attempt.status = PaymentStatus.FAILED.value
            payment_attempt.error_message = f"Service unavailable: {e}"
            await self.session.flush()
            return PaymentExecutionResult(
                success=False,
                payment_id=str(payment_attempt.id),
                status=PaymentStatus.FAILED.value,
                amount=amount,
                error_message=f"PaySwift unavailable: {e}",
            )

        except PaySwiftError as e:
            logger.error(f"PaySwift error: {e}")
            payment_attempt.status = PaymentStatus.FAILED.value
            payment_attempt.error_message = str(e)
            await self.session.flush()
            return PaymentExecutionResult(
                success=False,
                payment_id=str(payment_attempt.id),
                status=PaymentStatus.FAILED.value,
                amount=amount,
                error_message=str(e),
            )

        except Exception as e:
            logger.exception(f"Unexpected payment exception: {e}")
            payment_attempt.status = PaymentStatus.FAILED.value
            payment_attempt.error_message = str(e)
            await self.session.flush()
            return PaymentExecutionResult(
                success=False,
                payment_id=str(payment_attempt.id),
                status=PaymentStatus.FAILED.value,
                amount=amount,
                error_message=f"Internal error: {e}",
            )

    async def verify_payswift_payment(
        self,
        rider_id: str,
        payout_id: Optional[str] = None,
        reference: Optional[str] = None,
    ) -> PaymentVerificationResult:
        """Tool 13: Provider-side verification via PaySwift GET /v1/payouts."""
        try:
            record = await self.payswift_client.verify_transaction(
                payout_id=payout_id,
                reference=reference,
                rider_id=rider_id,
            )
            if record:
                return PaymentVerificationResult(
                    verified=True,
                    payout_id=record.get("payout_id"),
                    status=record.get("status"),
                    provider_record=record,
                )
            return PaymentVerificationResult(
                verified=False,
                payout_id=payout_id,
                status="not_found",
            )
        except Exception as e:
            logger.warning(f"Provider transaction verification failed: {e}")
            return PaymentVerificationResult(
                verified=False,
                payout_id=payout_id,
                status="verification_error",
                provider_record={"error": str(e)},
            )
