"""Audit trail helper for recording all agent actions."""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import AuditEvent

logger = logging.getLogger(__name__)


async def record_audit_event(
    session: AsyncSession,
    rider_id: str,
    event_type: str,
    name: str,
    input_data: Any = None,
    output_data: Any = None,
) -> AuditEvent:
    """Record an audit event for a rider.

    Args:
        session: Database session
        rider_id: The rider this event is for
        event_type: One of: message_in, tool_call, decision, reply, error
        name: Description of the action
        input_data: Input data (will be JSON-serialized)
        output_data: Output data (will be JSON-serialized)

    Returns:
        The created AuditEvent
    """
    def safe_json(data):
        if data is None:
            return None
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    event = AuditEvent(
        rider_id=rider_id,
        event_type=event_type,
        name=name,
        input_data=safe_json(input_data),
        output_data=safe_json(output_data),
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    await session.flush()

    logger.debug(f"Audit [{rider_id}] {event_type}: {name}")
    return event
