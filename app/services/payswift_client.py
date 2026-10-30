"""HTTP client for the PaySwift sandbox API.

Endpoints:
- POST /v1/payouts
- GET /v1/payouts?rider_id=<rider_id>
- GET /health

Handles timeouts, retries, idempotency keys, and transaction verification.
"""
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Dict, List, Optional, Union
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class PaySwiftError(Exception):
    """Base exception for PaySwift API errors."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PaySwift error {status_code}: {detail}")


class PaySwiftUnavailable(Exception):
    """PaySwift service is unreachable."""
    pass


class PaySwiftTimeout(Exception):
    """PaySwift request timed out."""
    pass


class PaySwiftRetryableError(PaySwiftError):
    """A provider error that is safe to retry with an idempotency key."""
    pass



class PaySwiftClient:
    """Client for the PaySwift Payouts API sandbox."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 10.0):
        self.base_url = (base_url or get_settings().PAYSWIFT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def health_check(self) -> bool:
        """Check if PaySwift is reachable and healthy (GET /health)."""
        client = self._get_client()
        try:
            resp = await client.get("/health")
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"PaySwift health check failed: {e}")
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(
            (PaySwiftUnavailable, PaySwiftTimeout, PaySwiftRetryableError)
        ),
        reraise=True,
    )
    async def create_payout(
        self,
        rider_id: str,
        amount: Union[int, Decimal, float],
        reference: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a payout in PaySwift (POST /v1/payouts).

        Args:
            rider_id: Rider to pay (e.g. "R003")
            amount: Whole rupees, 1 to 10000
            reference: Free text reference (e.g. unique dispute ID)
            idempotency_key: Optional header to ensure idempotent payment execution

        Returns:
            dict containing payout_id and status (e.g. {"payout_id": "...", "status": "paid"})
        """
        # Convert amount to integer rupee amount (1 - 10000)
        dec_amount = Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        int_amount = int(dec_amount)
        if int_amount < 1 or int_amount > 10000:
            raise ValueError(f"PaySwift amount must be between ₹1 and ₹10,000. Got: {int_amount}")

        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)

        payload = {
            "rider_id": rider_id,
            "amount": int_amount,
            "reference": reference,
        }

        # Safe logging without leaking secrets or auth headers
        logger.info(f"Submitting PaySwift payout for rider {rider_id}, amount ₹{int_amount}, ref: {reference}")

        client = self._get_client()
        try:
            resp = await client.post("/v1/payouts", json=payload, headers=headers)
        except httpx.TimeoutException as e:
            logger.error(f"PaySwift request timed out: {e}")
            raise PaySwiftTimeout(f"PaySwift request timed out: {e}")
        except httpx.ConnectError as e:
            logger.error(f"PaySwift connection failed: {e}")
            raise PaySwiftUnavailable(f"Cannot reach PaySwift: {e}")

        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info(f"PaySwift payout succeeded: id={data.get('payout_id')}, status={data.get('status')}")
            return data
        elif resp.status_code == 409:
            # Idempotency conflict: PaySwift returns existing payout info or error
            logger.info(f"PaySwift 409 conflict for idempotency key '{idempotency_key}': {resp.text}")
            try:
                return resp.json()
            except Exception:
                return {"status": "conflict", "detail": resp.text}
        else:
            detail = resp.text
            logger.error(f"PaySwift HTTP error {resp.status_code}: {detail}")
            if resp.status_code == 503 and idempotency_key:
                raise PaySwiftRetryableError(resp.status_code, detail)
            raise PaySwiftError(resp.status_code, detail)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((PaySwiftUnavailable, PaySwiftTimeout)),
        reraise=True,
    )
    async def get_payouts(self, rider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List payouts via GET /v1/payouts, optionally filtered by rider_id.

        Safe GET operation with retries.
        """
        params = {}
        if rider_id:
            params["rider_id"] = rider_id

        client = self._get_client()
        try:
            resp = await client.get("/v1/payouts", params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.error(f"PaySwift connection failed on GET: {e}")
            raise PaySwiftUnavailable(f"Cannot reach PaySwift: {e}")

        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                return payload["data"]
            raise PaySwiftError(
                resp.status_code,
                "Unexpected PaySwift payout-list response: expected a list or an object with a data list.",
            )
        else:
            logger.error(f"PaySwift GET error {resp.status_code}: {resp.text}")
            raise PaySwiftError(resp.status_code, resp.text)

    async def verify_transaction(
        self,
        payout_id: Optional[str] = None,
        reference: Optional[str] = None,
        rider_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Verify transaction against PaySwift provider records without inventing extra endpoints.

        Queries GET /v1/payouts and matches payout_id or reference.
        Returns the verified payout dict or None.
        """
        payouts = await self.get_payouts(rider_id=rider_id)
        for p in payouts:
            if payout_id and p.get("payout_id") == payout_id:
                return p
            if reference and p.get("reference") == reference:
                return p
        return None

    def generate_idempotency_key(self, dispute_id: str, suffix: str = "") -> str:
        """Deterministic idempotency key for dispute payment."""
        if suffix:
            return f"dsp-{dispute_id}-{suffix}"
        return f"dsp-{dispute_id}"
