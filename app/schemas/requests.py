"""Pydantic request/response schemas for QuickDrop API."""
from pydantic import BaseModel, Field
from typing import Optional, Any, List
from decimal import Decimal


# ── POST /messages ───────────────────────────────────────────────────

class MessageRequest(BaseModel):
    message_id: str = Field(..., description="Vendor message ID (wamid) for idempotency")
    rider_id: str = Field(..., description="Rider identifier from phone number")
    text: str = Field(..., description="Message text from rider")
    received_at: str = Field(..., description="ISO 8601 timestamp of when message was received")


class MessageResponse(BaseModel):
    reply: str
    message_id: str


# ── GET /trace/{rider_id} ───────────────────────────────────────────

class TraceStep(BaseModel):
    at: str  # ISO timestamp
    type: str  # message_in, tool_call, decision, reply, error
    name: str
    input: Optional[Any] = None
    output: Optional[Any] = None


# ── GET /ops/pending ─────────────────────────────────────────────────

class OpsPendingItem(BaseModel):
    id: str
    rider_id: str
    type: str  # approval or escalation
    amount: Optional[int] = None
    reason: str
    created_at: str


# ── Ops actions ──────────────────────────────────────────────────────

class ApprovalDecisionRequest(BaseModel):
    reviewer: str = Field(default="ops_admin", description="Name or ID of ops reviewer")
    notes: Optional[str] = Field(default=None, description="Optional decision notes")


class ApprovalDecisionResponse(BaseModel):
    id: str
    status: str
    message: str
    payswift_payout_id: Optional[str] = None
    amount_paid: Optional[Decimal] = None
