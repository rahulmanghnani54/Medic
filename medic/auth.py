"""Role gate for the three consoles.

This is deliberately minimal and is NOT production authentication: there is no
user identity, no session, no rotation and no rate limiting. It exists so the
pilot's three apps cannot call each other's endpoints by accident, and so every
audit row has an actor.

Before any real customer data or any real prescription touches this system,
replace it with per-user authentication (OTP for customers, staff accounts for
chemists and pharmacists) and put the pharmacist console behind SSO. Tracked in
docs/compliance.md.
"""

from __future__ import annotations

import hashlib

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import settings
from medic.models import Courier

Role = str


def _actor_from(token: str | None, expected: Role) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Medic-Token header",
        )
    if token != settings.role_tokens.get(expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this endpoint requires the {expected} role",
        )
    return expected


def require_role(expected: Role):
    def dependency(x_medic_token: str | None = Header(default=None)) -> str:
        return _actor_from(x_medic_token, expected)

    return Depends(dependency)


def courier_from_token(session: Session, token: str | None) -> Courier:
    """Resolve a courier from their own secret, not a shared role token.

    This one role cannot use the shared-token scheme above. The Schedule H1
    register records who took custody of a package, and a secret shared by
    the whole fleet cannot answer that question -- it would put a name on a
    statutory record that nothing authenticated.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-Medic-Token header"
        )
    digest = hashlib.sha256(token.encode()).hexdigest()
    courier = session.scalars(select(Courier).where(Courier.token_sha256 == digest)).first()
    if courier is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this endpoint requires a courier token"
        )
    if not courier.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="courier is not active")
    return courier
