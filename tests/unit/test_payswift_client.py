"""Unit tests for the PaySwift client with mocked sandbox responses."""
import pytest
import httpx
from decimal import Decimal
from pytest_httpx import HTTPXMock

from app.services.payswift_client import (
    PaySwiftClient,
    PaySwiftError,
    PaySwiftUnavailable,
)


@pytest.mark.asyncio
async def test_health_check_success(httpx_mock: HTTPXMock):
    """GET /health returns 200."""
    httpx_mock.add_response(url="http://payswift:8081/health", status_code=200, json={"status": "ok"})
    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        healthy = await client.health_check()
        assert healthy is True


@pytest.mark.asyncio
async def test_create_payout_success(httpx_mock: HTTPXMock):
    """POST /v1/payouts with valid rider, amount, and reference returns 201."""
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts",
        method="POST",
        status_code=201,
        json={"payout_id": "ps_12345", "status": "paid", "amount": 25, "rider_id": "R003"}
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        resp = await client.create_payout(
            rider_id="R003",
            amount=Decimal("25"),
            reference="dispute-dsp-001",
            idempotency_key="key-001",
        )
        assert resp["payout_id"] == "ps_12345"
        assert resp["status"] == "paid"

    request = httpx_mock.get_request()
    assert request.headers["Idempotency-Key"] == "key-001"


@pytest.mark.asyncio
async def test_create_payout_amount_limits():
    """Amounts below 1 or above 10000 must raise ValueError."""
    client = PaySwiftClient(base_url="http://payswift:8081")
    with pytest.raises(ValueError, match="between ₹1 and ₹10,000"):
        await client.create_payout(rider_id="R003", amount=0, reference="ref")
    with pytest.raises(ValueError, match="between ₹1 and ₹10,000"):
        await client.create_payout(rider_id="R003", amount=10001, reference="ref")


@pytest.mark.asyncio
async def test_create_payout_idempotency_conflict(httpx_mock: HTTPXMock):
    """POST /v1/payouts returning 409 conflict returns existing record."""
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts",
        method="POST",
        status_code=409,
        json={"payout_id": "ps_existing", "status": "paid", "detail": "Already processed"}
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        resp = await client.create_payout(
            rider_id="R003",
            amount=50,
            reference="ref-dup",
            idempotency_key="key-dup"
        )
        assert resp["payout_id"] == "ps_existing"


@pytest.mark.asyncio
async def test_create_payout_idempotency_replay_returns_original_payout(httpx_mock: HTTPXMock):
    """The sandbox returns HTTP 200 with the original payout on an idempotency replay."""
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts",
        method="POST",
        status_code=200,
        json={"payout_id": "ps_existing", "status": "processed", "amount": 1, "rider_id": "R017"},
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        response = await client.create_payout(
            rider_id="R017",
            amount=1,
            reference="audit",
            idempotency_key="key-dup",
        )

    assert response["payout_id"] == "ps_existing"
    assert response["status"] == "processed"


@pytest.mark.asyncio
async def test_create_payout_retries_503_with_idempotency_key(httpx_mock: HTTPXMock):
    """A temporary 503 is retried using the same safe idempotency key."""
    for status_code, payload in (
        (503, {"error": "service_unavailable"}),
        (201, {"payout_id": "ps_retried", "status": "processed", "amount": 150, "rider_id": "R005"}),
    ):
        httpx_mock.add_response(
            url="http://payswift:8081/v1/payouts",
            method="POST",
            status_code=status_code,
            json=payload,
        )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        response = await client.create_payout(
            rider_id="R005",
            amount=150,
            reference="daily-incentive",
            idempotency_key="stable-key",
        )

    requests = httpx_mock.get_requests()
    assert response["payout_id"] == "ps_retried"
    assert len(requests) == 2
    assert all(request.headers["Idempotency-Key"] == "stable-key" for request in requests)


@pytest.mark.asyncio
async def test_create_payout_does_not_retry_503_without_idempotency_key(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts",
        method="POST",
        status_code=503,
        json={"error": "service_unavailable"},
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        with pytest.raises(PaySwiftError) as error:
            await client.create_payout(
                rider_id="R005",
                amount=150,
                reference="daily-incentive",
            )

    assert error.value.status_code == 503
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_payouts_by_rider(httpx_mock: HTTPXMock):
    """GET /v1/payouts?rider_id=R017 returns list of payouts."""
    payout_list = [
        {"payout_id": "ps_1", "rider_id": "R017", "amount": 100, "status": "paid", "reference": "trip-T1"},
        {"payout_id": "ps_2", "rider_id": "R017", "amount": 50, "status": "paid", "reference": "trip-T2"},
    ]
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts?rider_id=R017",
        method="GET",
        status_code=200,
        json=payout_list
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        res = await client.get_payouts(rider_id="R017")
        assert len(res) == 2
        assert res[0]["payout_id"] == "ps_1"


@pytest.mark.asyncio
async def test_get_payouts_accepts_sandbox_data_envelope(httpx_mock: HTTPXMock):
    """The actual sandbox wraps payout arrays in a data/count response object."""
    payout = {
        "payout_id": "ps_wrapped",
        "rider_id": "R017",
        "amount": 1,
        "status": "processed",
        "reference": "audit",
    }
    for _ in range(2):
        httpx_mock.add_response(
            url="http://payswift:8081/v1/payouts?rider_id=R017",
            method="GET",
            status_code=200,
            json={"data": [payout], "count": 1},
        )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        res = await client.get_payouts(rider_id="R017")
        verified = await client.verify_transaction(payout_id="ps_wrapped", rider_id="R017")

    assert res == [payout]
    assert verified == payout


@pytest.mark.asyncio
async def test_verify_transaction(httpx_mock: HTTPXMock):
    """verify_transaction locates existing provider payout by payout_id or reference."""
    payout_list = [
        {"payout_id": "ps_target", "rider_id": "R003", "amount": 25, "status": "paid", "reference": "dispute-T926334"}
    ]
    httpx_mock.add_response(
        url="http://payswift:8081/v1/payouts?rider_id=R003",
        method="GET",
        status_code=200,
        json=payout_list
    )

    async with PaySwiftClient(base_url="http://payswift:8081") as client:
        verified = await client.verify_transaction(reference="dispute-T926334", rider_id="R003")
        assert verified is not None
        assert verified["payout_id"] == "ps_target"
        assert verified["status"] == "paid"
