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
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .header h1 { font-size: 24px; font-weight: 700; color: #fff; }
    .ops-login { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .ops-login input { width: 130px; padding: 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text); }
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
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .card h2 { font-size: 18px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
    th { color: var(--text-muted); padding: 10px; border-bottom: 1px solid var(--border); font-weight: 600; }
    td { padding: 12px 10px; border-bottom: 1px solid var(--border); }
    tr:hover { background: rgba(255, 255, 255, 0.02); }
    button {
      cursor: pointer;
      border: none;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      transition: all 0.2s;
    }
    .btn-approve { background: var(--success); color: #000; }
    .btn-approve:hover { filter: brightness(1.1); }
    .btn-reject { background: var(--danger); color: #fff; margin-left: 6px; }
    .btn-reject:hover { filter: brightness(1.1); }
    .btn-view { background: #334155; color: #fff; margin-left: 6px; }
    .btn-view:hover { background: #475569; }
    .btn-refresh { background: var(--primary); color: #000; padding: 8px 16px; border-radius: 8px; }
    .trace-item {
      padding: 10px;
      border-left: 2px solid var(--primary);
      margin-bottom: 8px;
      background: rgba(15, 23, 42, 0.4);
      border-radius: 0 6px 6px 0;
      font-size: 13px;
    }
    .trace-time { color: var(--text-muted); font-size: 11px; }
    .trace-type { font-weight: 600; color: var(--primary); }
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
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>QuickDrop Rider Payout Dispute Desk</h1>
      <p style="color: var(--text-muted); font-size: 14px;">Deterministic Reconciled Dispute Operations & Approvals</p>
    </div>
    <div>
      <div class="ops-login">
        <label for="opsUsername">Ops user</label>
        <input id="opsUsername" name="username" autocomplete="username" aria-label="Operations username">
        <label for="opsPassword">Password</label>
        <input id="opsPassword" name="password" type="password" autocomplete="current-password" aria-label="Operations password">
        <input id="opsDecisionNotes" name="decision-notes" aria-label="Decision notes" placeholder="Decision notes">
        <button class="btn-view" onclick="setOpsCredentials()">Set credentials</button>
        <span id="opsAuthStatus" role="status">Credentials required for decisions</span>
      </div>
      <button class="btn-refresh" onclick="loadPending()">Refresh Pending</button>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>
          <span>Pending Approvals & Escalations</span>
          <span id="pendingCount" class="badge badge-pending">0</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Rider</th>
              <th>Type</th>
              <th>Amount</th>
              <th>Reason</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="pendingTable">
            <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Loading pending cases...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div>
      <div class="card" id="detailPanel">
        <h2>Rider Audit Trace & Context: <span id="detailRider" style="color: var(--primary);"></span></h2>
        <div id="detailTrace" style="max-height: 480px; overflow-y: auto;"></div>
      </div>
      <div class="card" id="emptyDetail">
        <h2>Audit Trace Inspector</h2>
        <p style="color: var(--text-muted);">Select a pending case or view rider to inspect their full action history, reconciliation evidence, and messages.</p>
      </div>
    </div>
  </div>

  <script>
    let opsAuthorization = null;

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

    async function loadPending() {
      try {
        const res = await fetch('/ops/pending');
        const items = await res.json();
        const tbody = document.getElementById('pendingTable');
        document.getElementById('pendingCount').textContent = items.length;

        if (items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No pending approvals. All disputes clear!</td></tr>';
          return;
        }

        tbody.innerHTML = items.map(item => `
          <tr>
            <td><strong>${item.rider_id}</strong></td>
            <td><span class="badge ${item.type === 'approval' ? 'badge-pending' : 'badge-danger'}">${item.type}</span></td>
            <td><strong>${item.amount ? '₹' + item.amount : '—'}</strong></td>
            <td style="max-width: 220px; font-size: 13px;">${item.reason}</td>
            <td>
              <button class="btn-approve" onclick="approveCase('${item.id}', '${item.rider_id}')">Approve</button>
              <button class="btn-reject" onclick="rejectCase('${item.id}', '${item.rider_id}')">Reject</button>
              <button class="btn-view" onclick="viewTrace('${item.rider_id}')">Trace</button>
            </td>
          </tr>
        `).join('');
      } catch (err) {
        console.error('Failed to load pending:', err);
      }
    }

    async function approveCase(id, riderId) {
      if (!confirm(`Are you sure you want to approve this dispute for ${riderId}? PaySwift payment will be disbursed.`)) return;
      const authorization = getOpsAuthorization();
      if (!authorization) {
        document.getElementById('opsAuthStatus').textContent = 'Set operations credentials first';
        return;
      }
      try {
        const res = await fetch(`/ops/approvals/${id}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': authorization },
          body: JSON.stringify({ reviewer: 'ops_dashboard', notes: 'Approved via Ops Dashboard' })
        });
        const data = await res.json();
        if (!res.ok) {
          if (res.status === 401) {
            opsAuthorization = null;
            document.getElementById('opsAuthStatus').textContent = 'Invalid operations credentials';
          }
          alert('Error: ' + (data.detail || data.message));
        } else {
          document.getElementById('opsAuthStatus').textContent = 'Operations action completed';
          alert('Success: ' + data.message);
          loadPending();
          viewTrace(riderId);
        }
      } catch (err) {
        alert('Request failed: ' + err.message);
      }
    }

    async function rejectCase(id, riderId) {
      const reason = document.getElementById('opsDecisionNotes').value.trim();
      if (!reason) {
        document.getElementById('opsAuthStatus').textContent = 'Enter decision notes before rejecting';
        return;
      }
      const authorization = getOpsAuthorization();
      if (!authorization) {
        document.getElementById('opsAuthStatus').textContent = 'Set operations credentials first';
        return;
      }
      try {
        const res = await fetch(`/ops/approvals/${id}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': authorization },
          body: JSON.stringify({ reviewer: 'ops_dashboard', notes: reason })
        });
        const data = await res.json();
        if (!res.ok) {
          if (res.status === 401) {
            opsAuthorization = null;
            document.getElementById('opsAuthStatus').textContent = 'Invalid operations credentials';
          }
          alert('Error: ' + (data.detail || data.message));
        } else {
          document.getElementById('opsAuthStatus').textContent = 'Operations action completed';
          alert('Success: ' + data.message);
          loadPending();
          viewTrace(riderId);
        }
      } catch (err) {
        alert('Request failed: ' + err.message);
      }
    }

    async function viewTrace(riderId) {
      document.getElementById('emptyDetail').style.display = 'none';
      document.getElementById('detailPanel').style.display = 'block';
      document.getElementById('detailRider').textContent = riderId;
      const traceContainer = document.getElementById('detailTrace');
      traceContainer.innerHTML = '<p style="color: var(--text-muted);">Loading audit trace...</p>';

      try {
        const res = await fetch(`/trace/${riderId}`);
        const steps = await res.json();
        if (steps.length === 0) {
          traceContainer.innerHTML = '<p style="color: var(--text-muted);">No trace events recorded for this rider.</p>';
          return;
        }

        traceContainer.innerHTML = steps.map(s => `
          <div class="trace-item">
            <div class="trace-time">${new Date(s.at).toLocaleTimeString()} · <span class="trace-type">${s.type}</span> · <strong>${s.name}</strong></div>
            ${s.input ? '<pre>Input: ' + JSON.stringify(s.input, null, 2) + '</pre>' : ''}
            ${s.output ? '<pre>Output: ' + JSON.stringify(s.output, null, 2) + '</pre>' : ''}
          </div>
        `).join('');
      } catch (err) {
        traceContainer.innerHTML = '<p style="color: var(--danger);">Failed to load trace.</p>';
      }
    }

    // Initial load
    loadPending();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content, status_code=200)
