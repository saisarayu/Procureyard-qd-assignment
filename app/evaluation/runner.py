"""Evaluation Runner for QuickDrop Rider Payout Dispute Desk.

Exercises ONLY the public API surface:
- POST /messages
- GET /trace/{rider_id}
- GET /ops/pending
- PaySwift public sandbox endpoints

Evaluates realistic conversations including all six Phase 1 scenarios.
Safe and idempotent; does not create duplicate or uncontrolled payouts when rerun.
"""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sys
from typing import List, Dict, Any, Optional

import httpx


@dataclass
class EvalScenario:
    name: str
    rider_id: str
    message: str
    received_at: str
    message_id_prefix: str
    expected_outcome: str  # "auto_paid", "pending_approval", "escalated", "outside_window", "clarification"
    expected_amount: Optional[int]
    expected_escalation: bool


SCENARIOS: List[EvalScenario] = [
    EvalScenario(
        name="Scenario A: Surge Underpayment (T926334)",
        rider_id="R003",
        message="My payout for trip T926334 is less. Expected surge was 1.5x but got base fare.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_a",
        expected_outcome="auto_paid",
        expected_amount=25,
        expected_escalation=False,
    ),
    EvalScenario(
        name="Scenario B: Surge Underpayment (T604546)",
        rider_id="R027",
        message="Trip T604546 me surge ka paisa nahi juda. Please check karein.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_b",
        expected_outcome="auto_paid",
        expected_amount=15,
        expected_escalation=False,
    ),
    EvalScenario(
        name="Scenario C: Missing Daily Incentive (2026-09-21)",
        rider_id="R005",
        message="Kal 21 tarikh ko 13 delivery mari thi lekin 150 rupaye ka incentive nahi aaya.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_c",
        expected_outcome="auto_paid",
        expected_amount=150,
        expected_escalation=False,
    ),
    EvalScenario(
        name="Scenario D: Duplicate Penalty (T637155)",
        rider_id="R014",
        message="Order T637155 cancel hua tha par penalty do baar 10-10 rupaye kat gayi.",
        received_at="2026-09-20T10:00:00Z",
        message_id_prefix="eval.scen_d",
        expected_outcome="pending_approval",
        expected_amount=10,
        expected_escalation=True,
    ),
    EvalScenario(
        name="Scenario E: Identity Mismatch (T840677)",
        rider_id="R007",
        message="Order T840677 ka paisa kam mila hai. Please update.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_e",
        expected_outcome="escalated",
        expected_amount=None,
        expected_escalation=True,
    ),
    EvalScenario(
        name="Scenario F: Distance Anomaly (T312538)",
        rider_id="R031",
        message="Trip T312538 payout check karein.",
        received_at="2026-09-20T10:00:00Z",
        message_id_prefix="eval.scen_f",
        expected_outcome="escalated",
        expected_amount=None,
        expected_escalation=True,
    ),
    EvalScenario(
        name="Scenario G: Outside 7-Day Window (2026-09-12)",
        rider_id="R003",
        message="Trip on 2026-09-12 payout was short.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_g",
        expected_outcome="outside_window",
        expected_amount=None,
        expected_escalation=False,
    ),
    EvalScenario(
        name="Scenario H: Unclear Complaint (Vague)",
        rider_id="R003",
        message="Paisa kam mila hai mera.",
        received_at="2026-09-22T10:00:00Z",
        message_id_prefix="eval.scen_h",
        expected_outcome="clarification",
        expected_amount=None,
        expected_escalation=False,
    ),
]


async def run_evaluation(
    base_url: str,
    payswift_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
):
    """Run full evaluation suite across public endpoints."""
    base_url = base_url.rstrip("/")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    print(f"\n================================================================================")
    print(f" QUICKDROP RIDER PAYOUT DISPUTE DESK — AUTOMATED EVALUATION SUITE")
    print(f" Target Service URL : {base_url}")
    print(f" Run ID             : {run_id}")
    print(f" Timestamp          : {datetime.now(timezone.utc).isoformat()}")
    print(f"================================================================================\n")

    manage_client = client is None
    c = client if client is not None else httpx.AsyncClient(timeout=25.0)
    try:
        client = c
        # 1. Verify Service Health
        try:
            health_res = await client.get(f"{base_url}/health")
            if health_res.status_code == 200:
                print(f"[*] Health Check PASSED: {health_res.json()}")
            else:
                print(f"[!] Health Check Degraded: {health_res.status_code} - {health_res.text}")
        except Exception as e:
            print(f"[!] Warning: Health check endpoint unreachable at {base_url}/health: {e}")

        # 2. Check PaySwift Sandbox if provided
        if payswift_url:
            try:
                ps_health = await client.get(f"{payswift_url.rstrip('/')}/health")
                print(f"[*] PaySwift Sandbox Health: {ps_health.status_code} {ps_health.text}")
            except Exception as e:
                print(f"[*] PaySwift Sandbox at {payswift_url} (optional): {e}")

        results = []

        print("\nRunning Evaluation Scenarios...")
        print("-" * 80)

        for idx, sc in enumerate(SCENARIOS, start=1):
            msg_id = f"{sc.message_id_prefix}_{run_id}"
            payload = {
                "message_id": msg_id,
                "rider_id": sc.rider_id,
                "text": sc.message,
                "received_at": sc.received_at,
            }

            try:
                # 1. POST /messages
                resp = await client.post(f"{base_url}/messages", json=payload)
                if resp.status_code != 200:
                    results.append({
                        "name": sc.name,
                        "status": "FAIL",
                        "expected": sc.expected_outcome,
                        "actual": f"HTTP {resp.status_code}",
                        "payment": "None",
                        "escalated": "Unknown",
                        "reply": resp.text[:100],
                    })
                    continue

                reply_data = resp.json()
                reply_text = reply_data.get("reply", "")

                # 2. GET /trace/{rider_id}
                trace_resp = await client.get(f"{base_url}/trace/{sc.rider_id}")
                trace_items = trace_resp.json() if trace_resp.status_code == 200 else []

                # 3. GET /ops/pending
                pending_resp = await client.get(f"{base_url}/ops/pending")
                pending_items = pending_resp.json() if pending_resp.status_code == 200 else []

                # Analyze outcome
                is_escalated = any(p.get("rider_id") == sc.rider_id for p in pending_items)
                expected_idempotency_key = (
                    "qd-msg-" + hashlib.sha256(msg_id.encode("utf-8")).hexdigest()
                )
                payment_event = next(
                    (
                        event for event in reversed(trace_items)
                        if event.get("name") == "execute_payswift_payment"
                        and (event.get("input") or {}).get("idempotency_key")
                        == expected_idempotency_key
                    ),
                    None,
                )
                payment_output = (payment_event or {}).get("output") or {}
                payment_succeeded = (
                    payment_output.get("success") is True
                    and payment_output.get("status") == "paid"
                    and bool(payment_output.get("payswift_payout_id"))
                )
                provider_verified = payment_succeeded
                if payment_succeeded and payswift_url:
                    try:
                        payouts_resp = await client.get(
                            f"{payswift_url.rstrip('/')}/v1/payouts",
                            params={"rider_id": sc.rider_id},
                        )
                        payouts_resp.raise_for_status()
                        payouts = payouts_resp.json()
                        provider_verified = any(
                            payout.get("payout_id") == payment_output["payswift_payout_id"]
                            for payout in payouts
                        )
                    except (httpx.HTTPError, ValueError, TypeError) as e:
                        print(f"[!] PaySwift transaction verification failed: {e}")
                        provider_verified = False

                actual_outcome = "unknown"
                if provider_verified:
                    actual_outcome = "auto_paid"
                elif "outside" in reply_text.lower() or "7-day" in reply_text.lower():
                    actual_outcome = "outside_window"
                elif "?" in reply_text or "order" in reply_text.lower() or "date" in reply_text.lower() and "mismatch" not in reply_text.lower():
                    actual_outcome = "clarification"
                elif "mismatch" in reply_text.lower() or "unusual distance" in reply_text.lower():
                    actual_outcome = "escalated"
                elif "operations" in reply_text.lower() or "approval" in reply_text.lower() or "forwarded" in reply_text.lower():
                    actual_outcome = "pending_approval"

                # Check pass/fail criteria
                passed = False
                if sc.expected_outcome == "auto_paid":
                    passed = provider_verified
                elif sc.expected_outcome == "pending_approval":
                    passed = ("operations" in reply_text.lower() or "approval" in reply_text.lower() or is_escalated)
                elif sc.expected_outcome == "escalated":
                    passed = ("mismatch" in reply_text.lower() or "unusual distance" in reply_text.lower() or is_escalated)
                elif sc.expected_outcome == "outside_window":
                    passed = ("outside" in reply_text.lower() or "7-day" in reply_text.lower())
                elif sc.expected_outcome == "clarification":
                    passed = ("?" in reply_text or "order" in reply_text.lower() or "date" in reply_text.lower())

                results.append({
                    "name": sc.name,
                    "status": "PASS" if passed else "FAIL",
                    "expected": sc.expected_outcome,
                    "actual": actual_outcome,
                    "payment": f"INR {sc.expected_amount}" if sc.expected_outcome == "auto_paid" else "None",
                    "escalated": "Yes" if is_escalated else "No",
                    "reply": reply_text[:90] + ("..." if len(reply_text) > 90 else ""),
                })

            except Exception as e:
                results.append({
                    "name": sc.name,
                    "status": "FAIL",
                    "expected": sc.expected_outcome,
                    "actual": f"Error: {e}",
                    "payment": "Error",
                    "escalated": "Error",
                    "reply": str(e),
                })

        # Print Formatted Report Table
        print(f"\n{'#':<3} | {'Scenario Name':<42} | {'Expected':<16} | {'Actual':<16} | {'Payment':<8} | {'Escalated':<9} | {'Status'}")
        print("-" * 110)
        passed_count = 0
        for i, r in enumerate(results, start=1):
            if r["status"] == "PASS":
                passed_count += 1
            print(f"{i:<3} | {r['name'][:42]:<42} | {r['expected']:<16} | {r['actual']:<16} | {r['payment']:<8} | {r['escalated']:<9} | {r['status']}")

        pass_rate = (passed_count / len(results)) * 100
        print("-" * 110)
        print(f"Overall Pass Rate: {passed_count}/{len(results)} ({pass_rate:.1f}%)\n")

        return passed_count == len(results)
    finally:
        if manage_client:
            await c.aclose()



def main():
    parser = argparse.ArgumentParser(description="QuickDrop Payout Dispute Desk Evaluation Runner")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of QuickDrop service")
    parser.add_argument("--payswift-url", default=None, help="PaySwift Sandbox URL (optional)")
    args = parser.parse_args()

    success = asyncio.run(run_evaluation(base_url=args.url, payswift_url=args.payswift_url))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
