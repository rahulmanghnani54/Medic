"""The audit trail.

Append-only. Never updated, never deleted. Extracted into its own module so
that every service writes through one chokepoint rather than each growing its
own copy.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from medic.models import AuditEvent


def audit(
    session: Session,
    *,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: int,
    detail: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail,
        )
    )
    # Flush here rather than leaving it to the next write: the audit trail has
    # to be readable the moment the step it records has happened, and the last
    # step in a flow has no later write to piggyback on.
    session.flush()
