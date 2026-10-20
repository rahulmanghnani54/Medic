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

from fastapi import Depends, Header, HTTPException, status

from medic.config import settings

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
