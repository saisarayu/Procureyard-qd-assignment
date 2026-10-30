"""FastAPI endpoints for QuickDrop Rider Payout Dispute Desk.

Public Endpoints:
- POST /messages
- GET /trace/{rider_id}
- GET /ops/pending
- POST /ops/approvals/{approval_id}/approve
- POST /ops/approvals/{approval_id}/reject
- GET /ops/conversations/{rider_id}
- GET /dashboard (Ops Dashboard UI)
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from typing import List, Optional, Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.orchestrator import AgentOrchestrator, MessageProcessingInProgress
from app.core.audit import record_audit_event
from app.core.config import get_settings
from app.database.connection import get_db
from app.models.database import (
    ApprovalRequest, AuditEvent, Dispute, Message, PaymentAttempt,
    ApprovalStatus, DisputeStatus, PaymentStatus, OpsItemType
)
from app.schemas.requests import (
    MessageRequest, MessageResponse, TraceStep, OpsPendingItem,
    ApprovalDecisionRequest, ApprovalDecisionResponse,
)
from app.services.payswift_client import PaySwiftClient, PaySwiftError, PaySwiftUnavailable

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()
orchestrator = AgentOrchestrator()


# ── POST /messages ───────────────────────────────────────────────────

@router.post("/messages", response_model=MessageResponse, status_code=status.HTTP_200_OK)
async def post_message(
    req: MessageRequest,
    db: AsyncSession = Depends(get_db),
):
    """Receive and process a WhatsApp message from a QuickDrop rider.

    - Uses message_id as idempotency key.
    - Handles concurrent duplicate requests safely.
    - Reconciles deterministically and pays eligible shortfalls via PaySwift.
    """
    try:
        reply = await orchestrator.process_message(
            session=db,
            message_id=req.message_id,
            rider_id=req.rider_id,
            text=req.text,
            received_at=req.received_at,
        )
        return MessageResponse(reply=reply, message_id=req.message_id)
    except MessageProcessingInProgress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This message is still being processed. Retry shortly.",
            headers={"Retry-After": "1"},
        )
    except Exception as e:
        logger.exception(f"Error processing message {req.message_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process message: {str(e)}",
        )


# ── GET /trace/{rider_id} ───────────────────────────────────────────

@router.get("/trace/{rider_id}", response_model=List[TraceStep], status_code=status.HTTP_200_OK)
async def get_rider_trace(
    rider_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the ordered audit action history for a rider."""
    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.rider_id == rider_id)
        .order_by(AuditEvent.created_at.asc())
    )
    events = result.scalars().all()

    def parse_json_safely(val: Optional[str]) -> Any:
        if not val:
            return None
        try:
            return json.loads(val)
        except Exception:
            return val

    trace_steps = []
    for e in events:
        step = TraceStep(
            at=e.created_at.isoformat() if e.created_at else datetime.now(timezone.utc).isoformat(),
            type=e.event_type,
            name=e.name,
            input=parse_json_safely(e.input_data),
            output=parse_json_safely(e.output_data),
        )
        trace_steps.append(step)

    return trace_steps


# ── GET /ops/pending ─────────────────────────────────────────────────

@router.get("/ops/pending", response_model=List[OpsPendingItem], status_code=status.HTTP_200_OK)
async def get_ops_pending(
    db: AsyncSession = Depends(get_db),
):
    """Return all pending approvals and escalations for human review."""
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING.value)
        .order_by(ApprovalRequest.created_at.desc())
    )
    items = result.scalars().all()

    pending_list = []
    for item in items:
        pending_list.append(
            OpsPendingItem(
                id=str(item.id),
                rider_id=item.rider_id,
                type=item.type,
                amount=int(item.amount) if item.amount is not None else None,
                reason=item.reason,
                created_at=item.created_at.isoformat() if item.created_at else "",
            )
        )

    return pending_list


from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import base64

security = HTTPBasic(auto_error=False)


def require_ops_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Verify operations authentication via HTTP Basic or Authorization header."""
    if credentials:
        user_ok = secrets.compare_digest(credentials.username, settings.OPS_USERNAME)
        pass_ok = secrets.compare_digest(credentials.password, settings.OPS_PASSWORD)
        if user_ok and pass_ok:
            return credentials.username

    if authorization:
        raw_token = authorization.replace("Bearer ", "").replace("Basic ", "").strip()
        try:
            decoded = base64.b64decode(raw_token).decode("utf-8")
            if ":" in decoded:
                u, p = decoded.split(":", 1)
                if secrets.compare_digest(u, settings.OPS_USERNAME) and secrets.compare_digest(p, settings.OPS_PASSWORD):
                    return u
        except Exception:
            pass
        if secrets.compare_digest(raw_token, settings.OPS_PASSWORD):
            return settings.OPS_USERNAME

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Operations credentials required",
        headers={"WWW-Authenticate": "Basic"},
    )


# ── POST /ops/approvals/{approval_id}/approve ─────────────────────────

@router.post("/ops/approvals/{approval_id}/approve", response_model=ApprovalDecisionResponse)
async def approve_pending_request(
    approval_id: str,
    decision: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    current_user: str = Depends(require_ops_auth),
    db: AsyncSession = Depends(get_db),
):
    """Approve a pending human approval request and execute PaySwift payout.

    Revalidates safety rules:
    - Never executes if rider identity is mismatched.
    - Never executes if anomalous distance.
    - Prevents duplicate approval execution.
    """
    try:
        appr_uuid = UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid approval UUID format")

    result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == appr_uuid))
    approval = result.scalar_one_or_none()

    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval request {approval_id} not found")

    if approval.status != ApprovalStatus.PENDING.value:
        return ApprovalDecisionResponse(
            id=str(approval.id),
            status=approval.status,
            message=f"Approval request is already in '{approval.status}' state",
        )

    # Fetch associated dispute
    dispute_res = await db.execute(select(Dispute).where(Dispute.id == approval.dispute_id))
    dispute = dispute_res.scalar_one_or_none()

    # Safety checks: Never bypass identity mismatch or data anomalies
    if dispute and dispute.category == "identity_mismatch":
        raise HTTPException(
            status_code=400,
            detail="Cannot approve payment for identity mismatch dispute. R7 and R007 cannot be merged.",
        )
    if dispute and dispute.category == "distance_anomaly":
        raise HTTPException(
            status_code=400,
            detail="Cannot automatically execute payment for anomalous distance dispute.",
        )

    amount_to_pay = approval.amount or (dispute.shortfall if dispute else None)
    if not amount_to_pay or amount_to_pay <= Decimal("0"):
        # Zero or non-financial escalation approved
        approval.status = ApprovalStatus.APPROVED.value
        approval.reviewer = decision.reviewer
        approval.decision_notes = decision.notes
        approval.decided_at = datetime.now(timezone.utc)
        if dispute:
            dispute.status = DisputeStatus.APPROVED.value
        await db.commit()
        return ApprovalDecisionResponse(
            id=str(approval.id),
            status=ApprovalStatus.APPROVED.value,
            message="Escalation marked approved without financial payout.",
        )

    # Execute payment via PaySwift
    idempotency_key = f"qd-appr-{approval.id}"
    reference = f"ops-approved-dispute-{dispute.id if dispute else approval.id}"

    # Record payment attempt
    attempt = PaymentAttempt(
        dispute_id=dispute.id if dispute else approval.dispute_id,
        rider_id=approval.rider_id,
        amount=amount_to_pay,
        idempotency_key=idempotency_key,
        status=PaymentStatus.PROCESSING.value,
        reference=reference,
    )
    db.add(attempt)
    await db.flush()

    try:
        payswift = orchestrator.payswift
        ps_resp = await payswift.create_payout(
            rider_id=approval.rider_id,
            amount=int(amount_to_pay),
            reference=reference,
            idempotency_key=idempotency_key,
        )
        payout_id = ps_resp.get("payout_id")

        attempt.payswift_payout_id = payout_id
        attempt.payswift_status = ps_resp.get("status", "paid")
        attempt.status = PaymentStatus.PAID.value
        attempt.verified_at = datetime.now(timezone.utc)

        approval.status = ApprovalStatus.APPROVED.value
        approval.reviewer = decision.reviewer
        approval.decision_notes = decision.notes
        approval.decided_at = datetime.now(timezone.utc)

        if dispute:
            dispute.status = DisputeStatus.PAID.value

        await record_audit_event(
            db, approval.rider_id, "decision", "ops_approval_executed",
            {"approval_id": str(approval.id), "amount": str(amount_to_pay), "reviewer": decision.reviewer},
            {"payout_id": payout_id, "status": "approved_and_paid"}
        )
        await db.commit()

        return ApprovalDecisionResponse(
            id=str(approval.id),
            status=ApprovalStatus.APPROVED.value,
            message=f"Approved and payment of ₹{amount_to_pay} disbursed successfully via PaySwift.",
            payswift_payout_id=payout_id,
            amount_paid=amount_to_pay,
        )

    except Exception as e:
        logger.exception(f"Error executing approved payout: {e}")
        attempt.status = PaymentStatus.FAILED.value
        attempt.error_message = str(e)
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"PaySwift payout execution failed: {str(e)}",
        )


# ── POST /ops/approvals/{approval_id}/reject ──────────────────────────

@router.post("/ops/approvals/{approval_id}/reject", response_model=ApprovalDecisionResponse)
async def reject_pending_request(
    approval_id: str,
    decision: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    current_user: str = Depends(require_ops_auth),
    db: AsyncSession = Depends(get_db),
):
    """Reject a pending human approval request."""
    try:
        appr_uuid = UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid approval UUID format")

    result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == appr_uuid))
    approval = result.scalar_one_or_none()

    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval request {approval_id} not found")

    if approval.status != ApprovalStatus.PENDING.value:
        return ApprovalDecisionResponse(
            id=str(approval.id),
            status=approval.status,
            message=f"Approval request is already in '{approval.status}' state",
        )

    approval.status = ApprovalStatus.REJECTED.value
    approval.reviewer = decision.reviewer
    approval.decision_notes = decision.notes or "Rejected by operations"
    approval.decided_at = datetime.now(timezone.utc)

    # Update dispute
    dispute_res = await db.execute(select(Dispute).where(Dispute.id == approval.dispute_id))
    dispute = dispute_res.scalar_one_or_none()
    if dispute:
        dispute.status = DisputeStatus.REJECTED.value

    await record_audit_event(
        db, approval.rider_id, "decision", "ops_approval_rejected",
        {"approval_id": str(approval.id), "reviewer": decision.reviewer, "notes": decision.notes},
        {"status": "rejected"}
    )
    await db.commit()

    return ApprovalDecisionResponse(
        id=str(approval.id),
        status=ApprovalStatus.REJECTED.value,
        message="Approval request rejected successfully.",
    )


# ── GET /ops/conversations/{rider_id} ─────────────────────────────────

@router.get("/ops/conversations/{rider_id}", status_code=status.HTTP_200_OK)
async def get_rider_conversations(
    rider_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return all messages and replies for a rider."""
    result = await db.execute(
        select(Message)
        .where(Message.rider_id == rider_id)
        .order_by(Message.received_at.asc())
    )
    messages = result.scalars().all()
    return [
        {
            "message_id": m.message_id,
            "rider_id": m.rider_id,
            "text": m.text,
            "reply": m.reply,
            "received_at": m.received_at.isoformat() if m.received_at else None,
            "processed": m.processed,
        }
        for m in messages
    ]


# ── GET /dashboard (Ops Dashboard UI) ────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
@router.get("/ops", response_class=HTMLResponse, tags=["Dashboard"])
async def ops_dashboard():
    """Minimal, usable operations dashboard for reviewing disputes and approving cases."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QuickDrop Payout Dispute Desk - Ops Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0284c7;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 24px;
    }
    .page { max-width: 1600px; margin: 0 auto; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      padding-bottom: 22px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 22px;
    }
    .header h1 { font-size: clamp(22px, 3vw, 30px); font-weight: 700; color: #fff; }
    .subtitle { color: var(--text-muted); margin-top: 5px; }
    .header-tools { display: grid; gap: 12px; min-width: min(100%, 520px); }
    .ops-login { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .ops-login input, .resolution-fields input, .resolution-fields textarea, .trace-search input {
      width: 100%; padding: 9px 10px; border: 1px solid var(--border); border-radius: 7px;
      background: var(--bg); color: var(--text); font: inherit;
    }
    .ops-login input { width: 140px; min-width: 110px; }
    .ops-login input:focus, .resolution-fields input:focus, .resolution-fields textarea:focus, .trace-search input:focus {
      outline: 2px solid var(--primary); outline-offset: 1px;
    }
    .toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .toolbar .btn-demo { margin-left: auto; }
    .auth-status { color: var(--text-muted); font-size: 12px; }
    .badge {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-pending { background: rgba(245, 158, 11, 0.2); color: var(--warning); border: 1px solid var(--warning); }
    .badge-approved { background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid var(--success); }
    .badge-danger { background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid var(--danger); }
    .badge-live { background: rgba(34, 197, 94, 0.12); color: #86efac; border: 1px solid rgba(34, 197, 94, 0.5); }
    .grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(340px, .85fr); gap: 20px; align-items: start; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
      min-width: 0;
    }
    .card h2 { font-size: 18px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .section-kicker { color: var(--primary); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 5px; }
    .section-copy { color: var(--text-muted); font-size: 13px; margin: -5px 0 16px; }
    .metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }
    .metric { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
    .metric-label { display: block; color: var(--text-muted); font-size: 12px; }
    .metric-value { display: block; font-size: 22px; font-weight: 700; margin-top: 2px; }
    .table-wrap { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; min-width: 650px; }
    th { color: var(--text-muted); padding: 10px; border-bottom: 1px solid var(--border); font-weight: 600; white-space: nowrap; }
    td { padding: 12px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
    tr:hover { background: rgba(255, 255, 255, 0.02); }
    button {
      cursor: pointer;
      border: none;
      padding: 8px 11px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      transition: all 0.2s;
    }
    button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
    button:disabled { cursor: wait; opacity: .6; }
    .btn-approve { background: var(--success); color: #000; }
    .btn-approve:hover { filter: brightness(1.1); }
    .btn-reject { background: var(--danger); color: #fff; }
    .btn-reject:hover { filter: brightness(1.1); }
    .btn-view { background: #334155; color: #fff; }
    .btn-view:hover { background: #475569; }
    .btn-refresh { background: var(--primary); color: #000; padding: 8px 16px; border-radius: 8px; }
    .btn-demo { background: #f59e0b; color: #000; margin-left: 8px; padding: 8px 16px; border-radius: 8px; }
    .demo-notice { display: none; color: #fde68a; font-size: 13px; font-weight: 600; margin-top: 8px; }
    .source-label { font-size: 11px; color: var(--text-muted); margin-top: 4px; display: block; }
    .case-actions { display: flex; flex-wrap: wrap; gap: 6px; }
    .resolution-fields { display: grid; grid-template-columns: minmax(130px, .4fr) minmax(220px, 1fr); gap: 12px; margin: 16px 0 10px; }
    .field { display: grid; gap: 6px; }
    .field label { color: var(--text-muted); font-size: 12px; font-weight: 600; }
    .resolution-fields textarea { min-height: 72px; resize: vertical; }
    .action-hint { color: var(--text-muted); font-size: 12px; }
    .trace-search { display: flex; gap: 8px; margin-bottom: 16px; }
    .trace-search input { min-width: 0; }
    .trace-search button { flex: 0 0 auto; }
    .trace-section-title { color: var(--text-muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; margin: 16px 0 9px; }
    .conversation-item { padding: 10px; border: 1px solid var(--border); border-radius: 8px; margin: 8px 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
    .conversation-item strong { color: var(--primary); }
    .empty-state, .error-state { text-align: center; padding: 24px 12px; color: var(--text-muted); }
    .error-state { color: #fca5a5; }
    .trace-item {
      padding: 10px;
      border-left: 2px solid var(--primary);
      margin-bottom: 8px;
      background: rgba(15, 23, 42, 0.4);
      border-radius: 0 6px 6px 0;
      font-size: 13px;
    }
    .trace-time { color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
    .trace-type { font-weight: 600; color: var(--primary); }
    .trace-item pre { white-space: pre-wrap; overflow-wrap: anywhere; }
    pre {
      background: #090d16;
      padding: 10px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 12px;
      color: #38bdf8;
      margin-top: 6px;
    }
    #detailPanel { display: none; }
    @media (max-width: 1000px) {
      body { padding: 18px; }
      .header { flex-direction: column; }
      .header-tools { width: 100%; }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      body { padding: 12px; }
      .card { padding: 15px; }
      .toolbar .btn-demo { margin-left: 0; }
      .metrics { gap: 7px; }
      .metric { padding: 11px 9px; }
      .metric-value { font-size: 18px; }
      .resolution-fields { grid-template-columns: 1fr; }
      .trace-search { flex-wrap: wrap; }
      .trace-search button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header class="header">
      <div>
        <h1>QuickDrop Rider Payout Dispute Desk</h1>
        <p class="subtitle">Reconciled disputes, investigation traces, and operations decisions</p>
        <p id="demoModeNotice" class="demo-notice">DEMO MODE — sample cases only; actions are simulated and no payment will be sent.</p>
      </div>
      <div class="header-tools">
        <div class="ops-login">
          <label for="opsUsername">Ops user</label>
          <input id="opsUsername" name="username" autocomplete="username" aria-label="Operations username">
          <label for="opsPassword">Password</label>
          <input id="opsPassword" name="password" type="password" autocomplete="current-password" aria-label="Operations password">
          <button class="btn-view" id="setCredentialsButton" type="button">Set credentials</button>
        </div>
        <div class="toolbar">
          <button class="btn-refresh" id="refreshPendingButton" type="button">Refresh cases</button>
          <button id="demoModeToggle" class="btn-demo" type="button">Show mock cases</button>
          <span id="opsAuthStatus" class="auth-status" role="status">Live cases; credentials required for decisions</span>
        </div>
      </div>
    </header>

    <section class="metrics" aria-label="Pending case summary">
      <div class="metric"><span class="metric-label">Pending approvals</span><strong id="approvalMetric" class="metric-value">—</strong></div>
      <div class="metric"><span class="metric-label">Escalations</span><strong id="escalationMetric" class="metric-value">—</strong></div>
      <div class="metric"><span class="metric-label">Known amount under review</span><strong id="amountMetric" class="metric-value">—</strong></div>
    </section>

    <main class="grid">
      <section class="card" aria-labelledby="pendingHeading">
        <p class="section-kicker">Workflow 1 · Pending approvals</p>
        <h2 id="pendingHeading">
          <span>Disputes needing review</span>
          <span id="pendingCount" class="badge badge-pending">0</span>
        </h2>
        <p class="section-copy">Review the rider, disputed amount, and reason before resolving a case.</p>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rider</th>
                <th>Type</th>
                <th>Amount</th>
                <th>Escalation reason</th>
                <th>Received</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="pendingTable">
              <tr><td colspan="6" class="empty-state">Loading live cases…</td></tr>
            </tbody>
          </table>
        </div>
        <p class="section-kicker" style="margin-top: 20px;">Workflow 3 · Case resolution</p>
        <div class="resolution-fields">
          <div class="field">
            <label for="opsReviewer">Reviewer</label>
            <input id="opsReviewer" name="reviewer" autocomplete="name" placeholder="Reviewer name">
          </div>
          <div class="field">
            <label for="opsDecisionNotes">Decision notes (required)</label>
            <textarea id="opsDecisionNotes" name="decision-notes" placeholder="Explain why this case is approved or rejected"></textarea>
          </div>
        </div>
        <p class="action-hint" id="resolutionHint">Approval may initiate a PaySwift payout when financially eligible. Verify the case and notes before resolving.</p>
      </section>

      <section class="card" aria-labelledby="traceHeading">
        <p class="section-kicker">Workflow 2 · Audit trace</p>
        <h2 id="traceHeading">Inspect an investigation</h2>
        <p class="section-copy">Search any rider to view their message history and the agent’s reconciliation steps.</p>
        <form class="trace-search" id="traceSearchForm">
          <input id="traceRiderId" name="rider-id" aria-label="Rider ID" placeholder="Enter rider ID, e.g. R014" autocomplete="off">
          <button class="btn-refresh" type="submit">Inspect rider</button>
        </form>
        <div id="emptyDetail" class="empty-state">Choose Trace on a pending case or enter a rider ID to inspect their activity.</div>
        <div id="detailPanel">
          <h2>Rider: <span id="detailRider" style="color: var(--primary);"></span></h2>
          <span id="traceSource" class="badge badge-live">Live backend data</span>
          <div id="detailMessages"></div>
          <div id="detailTrace"></div>
        </div>
      </section>
    </main>
  </div>

  <script>
    let opsAuthorization = null;
    let demoMode = new URLSearchParams(window.location.search).get('demo') === '1';
    let displayedDemoMode = null;
    const demoPendingItems = [
      {
        id: 'mock-approval-r014',
        rider_id: 'MOCK-R014',
        type: 'approval',
        amount: 350,
        reason: 'Sample shortfall requiring operations review'
      },
      {
        id: 'mock-escalation-r031',
        rider_id: 'MOCK-R031',
        type: 'escalation',
        amount: 5000,
        reason: 'Sample distance anomaly; verify before any payout'
      },
      {
        id: 'mock-approval-r007',
        rider_id: 'MOCK-R007',
        type: 'approval',
        amount: 120,
        reason: 'Sample repeat dispute requiring manual review'
      }
    ];
    const demoDecisions = {};

    function getOpsAuthorization() {
      return opsAuthorization;
    }

    function setOpsCredentials() {
      const username = document.getElementById('opsUsername').value;
      const password = document.getElementById('opsPassword').value;
      const status = document.getElementById('opsAuthStatus');
      if (!username || !password) {
        opsAuthorization = null;
        status.textContent = 'Enter both operations credentials';
        return;
      }
      opsAuthorization = `Basic ${btoa(`${username}:${password}`)}`;
      status.textContent = 'Credentials set; access checked on action';
    }

    function toggleDemoMode() {
      demoMode = !demoMode;
      const params = new URLSearchParams(window.location.search);
      if (demoMode) {
        params.set('demo', '1');
      } else {
        params.delete('demo');
      }
      const query = params.toString();
      window.history.replaceState(null, '', `${window.location.pathname}${query ? '?' + query : ''}`);
      displayedDemoMode = null;
      loadPending();
    }

    let pendingItems = [];

    function updateDemoModeIndicator() {
      document.getElementById('demoModeNotice').style.display = demoMode ? 'block' : 'none';
      document.getElementById('demoModeToggle').textContent = demoMode ? 'Return to live cases' : 'Show mock cases';
      if (displayedDemoMode !== demoMode) {
        document.getElementById('opsAuthStatus').textContent = demoMode
          ? 'Demo mode: actions are local simulations only'
          : (opsAuthorization ? 'Credentials set; access checked when resolving' : 'Live cases; credentials required for decisions');
        displayedDemoMode = demoMode;
      }
      document.getElementById('resolutionHint').textContent = demoMode
        ? 'Demo mode: decisions are simulated locally and never reach the backend or PaySwift.'
        : 'Approval may initiate a PaySwift payout when financially eligible. Verify the case and notes before resolving.';
      document.getElementById('traceSource').textContent = demoMode ? 'Demo-only sample' : 'Live backend data';
      document.getElementById('traceSource').className = `badge ${demoMode ? 'badge-pending' : 'badge-live'}`;
    }

    function escapeHTML(value) {
      return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[character]);
    }

    function formatTimestamp(value) {
      if (!value) return '—';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
    }

    function renderSummary(items) {
      const approvals = items.filter(item => item.type === 'approval').length;
      const escalations = items.filter(item => item.type === 'escalation').length;
      const amount = items.reduce((total, item) => total + (Number(item.amount) || 0), 0);
      document.getElementById('pendingCount').textContent = items.length;
      document.getElementById('approvalMetric').textContent = approvals;
      document.getElementById('escalationMetric').textContent = escalations;
      document.getElementById('amountMetric').textContent = `₹${amount.toLocaleString('en-IN')}`;
    }

    function renderPending(items) {
      const tbody = document.getElementById('pendingTable');
      renderSummary(items);
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No live cases are awaiting operations review. Rider audit traces remain searchable.</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(item => `
        <tr>
          <td><strong>${escapeHTML(item.rider_id)}</strong></td>
          <td><span class="badge ${item.type === 'approval' ? 'badge-pending' : 'badge-danger'}">${escapeHTML(item.type)}</span></td>
          <td><strong>${item.amount !== null && item.amount !== undefined ? '₹' + escapeHTML(item.amount) : '—'}</strong></td>
          <td>${escapeHTML(item.reason)}</td>
          <td>${escapeHTML(formatTimestamp(item.created_at))}</td>
          <td><div class="case-actions">
            <button class="btn-approve" type="button" data-action="approve" data-id="${escapeHTML(item.id)}" data-rider="${escapeHTML(item.rider_id)}">Approve</button>
            <button class="btn-reject" type="button" data-action="reject" data-id="${escapeHTML(item.id)}" data-rider="${escapeHTML(item.rider_id)}">Reject</button>
            <button class="btn-view" type="button" data-action="trace" data-rider="${escapeHTML(item.rider_id)}">Trace</button>
          </div></td>
        </tr>
      `).join('');
    }

    async function loadPending() {
      updateDemoModeIndicator();
      try {
        let items;
        if (demoMode) {
          items = demoPendingItems.map(item => ({ ...item, created_at: new Date().toISOString() }));
        } else {
          const res = await fetch('/ops/pending');
          if (!res.ok) throw new Error(`Pending cases request failed (${res.status})`);
          items = await res.json();
        }
        pendingItems = items;
        renderPending(items);
      } catch (err) {
        document.getElementById('pendingTable').innerHTML =
          `<tr><td colspan="6" class="error-state">Could not load live cases: ${escapeHTML(err.message)}</td></tr>`;
        document.getElementById('opsAuthStatus').textContent = 'Failed to load pending cases';
      }
    }

    function simulateDemoDecision(id, riderId, decision, notes) {
      demoDecisions[riderId] = {
        at: new Date().toISOString(),
        decision,
        notes
      };
      const index = demoPendingItems.findIndex(item => item.id === id);
      if (index !== -1) demoPendingItems.splice(index, 1);
      loadPending();
      viewTrace(riderId);
      document.getElementById('opsAuthStatus').textContent =
        `Mock ${decision} recorded locally; no payment was sent`;
    }

    async function resolveCase(action, id, riderId) {
      const reviewer = document.getElementById('opsReviewer').value.trim();
      const notes = document.getElementById('opsDecisionNotes').value.trim();
      const status = document.getElementById('opsAuthStatus');
      if (!reviewer || !notes) {
        status.textContent = 'Enter reviewer name and decision notes before resolving';
        return;
      }
      if (demoMode) {
        if (!confirm(`Demo only: simulate ${action} for ${riderId}? No live case or payment will be changed.`)) return;
        simulateDemoDecision(id, riderId, action === 'approve' ? 'approved' : 'rejected', notes);
        status.textContent = `Mock ${action} recorded locally; no payment was sent`;
        return;
      }
      const authorization = getOpsAuthorization();
      if (!authorization) {
        status.textContent = 'Set operations credentials first';
        return;
      }
      const caseItem = pendingItems.find(item => item.id === id);
      if (!confirm(`${action === 'approve' ? 'Approve' : 'Reject'} the case for ${riderId}?${action === 'approve' && Number(caseItem?.amount) > 0 ? ` This may disburse ₹${caseItem.amount} through PaySwift.` : ''}`)) return;
      try {
        const res = await fetch(`/ops/approvals/${encodeURIComponent(id)}/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': authorization },
          body: JSON.stringify({ reviewer, notes })
        });
        const data = await res.json();
        if (!res.ok) {
          if (res.status === 401) {
            opsAuthorization = null;
            status.textContent = 'Invalid operations credentials';
          }
          throw new Error(data.detail || data.message || `Operation failed (${res.status})`);
        } else {
          status.textContent = data.message;
          await loadPending();
          await viewTrace(riderId);
        }
      } catch (err) {
        status.textContent = `Operation failed: ${err.message}`;
      }
    }

    async function viewTrace(riderId) {
      riderId = riderId.trim();
      if (!riderId) return;
      document.getElementById('emptyDetail').style.display = 'none';
      document.getElementById('detailPanel').style.display = 'block';
      document.getElementById('detailRider').textContent = riderId;
      const traceContainer = document.getElementById('detailTrace');
      const messagesContainer = document.getElementById('detailMessages');
      traceContainer.innerHTML = '<p class="empty-state">Loading investigation events…</p>';
      messagesContainer.innerHTML = '';
      updateDemoModeIndicator();

      try {
        let steps = [];
        let messages = [];
        if (demoMode) {
          steps = [{
            at: new Date().toISOString(),
            type: 'demo',
            name: 'Sample dispute context',
            input: { rider_id: riderId },
            output: { source: 'mock fixture', payment_sent: false }
          }];
          if (demoDecisions[riderId]) {
            steps.push({
              at: demoDecisions[riderId].at,
              type: 'demo',
              name: 'Simulated operations decision',
              input: { decision: demoDecisions[riderId].decision, notes: demoDecisions[riderId].notes },
              output: { persisted: false, payment_sent: false }
            });
          }
        } else {
          const [traceRes, messagesRes] = await Promise.all([
            fetch(`/trace/${encodeURIComponent(riderId)}`),
            fetch(`/ops/conversations/${encodeURIComponent(riderId)}`)
          ]);
          if (!traceRes.ok) throw new Error(`Rider trace request failed (${traceRes.status})`);
          if (!messagesRes.ok) throw new Error(`Rider conversation request failed (${messagesRes.status})`);
          [steps, messages] = await Promise.all([traceRes.json(), messagesRes.json()]);
        }

        messagesContainer.innerHTML = messages.length ? `
          <h3 class="trace-section-title">Rider messages and agent replies</h3>
          ${messages.map(message => `
            <div class="conversation-item">
              <strong>${escapeHTML(message.rider_id)} · ${escapeHTML(formatTimestamp(message.received_at))}</strong>
              <span class="source-label">Message ${escapeHTML(message.message_id)}</span>
              <div>${escapeHTML(message.text)}</div>
              ${message.reply ? `<div><strong>Agent reply:</strong> ${escapeHTML(message.reply)}</div>` : ''}
            </div>
          `).join('')}
        ` : '';

        if (!steps.length) {
          traceContainer.innerHTML = '<p class="empty-state">No audit trace events recorded for this rider.</p>';
          return;
        }

        traceContainer.innerHTML = `<h3 class="trace-section-title">Agent investigation timeline</h3>` + steps.map(s => `
          <div class="trace-item">
            <div class="trace-time">${escapeHTML(formatTimestamp(s.at))} · <span class="trace-type">${escapeHTML(s.type)}</span> · <strong>${escapeHTML(s.name)}</strong></div>
            ${s.input ? `<pre>Input: ${escapeHTML(JSON.stringify(s.input, null, 2))}</pre>` : ''}
            ${s.output ? `<pre>Output: ${escapeHTML(JSON.stringify(s.output, null, 2))}</pre>` : ''}
          </div>
        `).join('');
      } catch (err) {
        messagesContainer.innerHTML = '';
        traceContainer.innerHTML = `<p class="error-state">Could not load rider investigation: ${escapeHTML(err.message)}</p>`;
      }
    }

    document.getElementById('setCredentialsButton').addEventListener('click', setOpsCredentials);
    document.getElementById('refreshPendingButton').addEventListener('click', loadPending);
    document.getElementById('demoModeToggle').addEventListener('click', toggleDemoMode);
    document.getElementById('pendingTable').addEventListener('click', event => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const { action, id, rider } = button.dataset;
      if (action === 'trace') viewTrace(rider);
      else resolveCase(action, id, rider);
    });
    document.getElementById('traceSearchForm').addEventListener('submit', event => {
      event.preventDefault();
      viewTrace(document.getElementById('traceRiderId').value);
    });

    updateDemoModeIndicator();
    loadPending();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content, status_code=200)
