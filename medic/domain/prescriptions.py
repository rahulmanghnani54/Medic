"""Prescription verification rules.

The platform never decides clinical questions. A pharmacist decides, and this
module's job is to make sure they decide with the right information in front of
them, and that the decision is then enforced mechanically:

- a prescription that has not been verified cannot release a scheduled drug;
- a verified prescription authorises specific medicines in specific quantities,
  and we dispense no more than that;
- the same prescription image resurfacing is surfaced to the pharmacist rather
  than silently accepted, which is the main abuse route for sleeping pills and
  other habit-forming drugs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from medic.config import Settings, settings
from medic.domain.schedules import DrugSchedule, policy_for


def image_fingerprint(image_bytes: bytes) -> str:
    """Content hash used for exact-duplicate detection.

    A re-photographed or lightly edited prescription will not collide. Catching
    those needs perceptual hashing or OCR, which is noted in docs/compliance.md
    as deliberately out of MVP scope -- so this is a floor on abuse detection,
    not a ceiling.
    """
    return hashlib.sha256(image_bytes).hexdigest()


class PrescriptionProblem(Exception):
    """A prescription cannot back this basket."""


@dataclass(frozen=True)
class Authorisation:
    medicine_id: int
    authorised_qty: int
    dispensed_qty: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.authorised_qty - self.dispensed_qty)


@dataclass(frozen=True)
class PrescriptionView:
    """What the rules need to know about a prescription."""

    id: int
    customer_id: int
    is_verified: bool
    issued_on: datetime | None
    max_uses: int
    uses_count: int
    authorisations: tuple[Authorisation, ...] = field(default_factory=tuple)

    def authorisation_for(self, medicine_id: int) -> Authorisation | None:
        return next((a for a in self.authorisations if a.medicine_id == medicine_id), None)


@dataclass(frozen=True)
class RequestedLine:
    medicine_id: int
    medicine_name: str
    qty: int
    schedule: DrugSchedule


def check_prescription_covers(
    lines: list[RequestedLine],
    prescription: PrescriptionView | None,
    *,
    customer_id: int,
    now: datetime,
    config: Settings = settings,
) -> None:
    """Raise PrescriptionProblem unless this prescription may release these lines.

    Lines that need no prescription are ignored, so a mixed basket passes on the
    strength of its scheduled lines alone.
    """
    controlled = [line for line in lines if policy_for(line.schedule).requires_prescription]
    if not controlled:
        return

    if prescription is None:
        names = ", ".join(line.medicine_name for line in controlled)
        raise PrescriptionProblem(f"a prescription is required for: {names}")

    if prescription.customer_id != customer_id:
        raise PrescriptionProblem("this prescription belongs to a different customer")

    if not prescription.is_verified:
        raise PrescriptionProblem("this prescription has not been verified by a pharmacist yet")

    if prescription.uses_count >= prescription.max_uses:
        raise PrescriptionProblem(
            f"this prescription has already been used {prescription.uses_count} times, "
            f"which is its limit"
        )

    if prescription.issued_on is not None:
        age = now - prescription.issued_on
        if age > timedelta(days=config.prescription_validity_days):
            raise PrescriptionProblem(
                f"this prescription is {age.days} days old, beyond the "
                f"{config.prescription_validity_days}-day limit for scheduled drugs"
            )

    for line in controlled:
        auth = prescription.authorisation_for(line.medicine_id)
        if auth is None:
            raise PrescriptionProblem(
                f"the verified prescription does not cover {line.medicine_name}"
            )
        if line.qty > auth.remaining:
            raise PrescriptionProblem(
                f"{line.medicine_name}: {line.qty} requested but only {auth.remaining} "
                f"remaining on this prescription"
            )
