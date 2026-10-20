"""Drug schedule policy.

This module is the single place where "what may we dispense, and under what
controls" is decided. Everything else in the platform asks this module rather
than hard-coding rules of its own.

IMPORTANT -- this policy is NOT legal advice and has NOT been reviewed by
counsel. It encodes the handling described in docs/venture-brief.md section 4,
which that document itself marks "to be confirmed by counsel". India's
e-pharmacy rules were still unsettled when the brief was written. Before
launch, every row of POLICY must be confirmed against the current central
rules and Rajasthan state drug-licensing requirements. See docs/compliance.md
for the open questions.

Design rule: fail closed. An unknown or unclassified medicine is never
dispensable, and a stricter control is never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DrugSchedule(StrEnum):
    """Schedules under the Drugs and Cosmetics Rules, as used by this platform."""

    OTC = "OTC"
    H = "H"
    H1 = "H1"
    X = "X"


@dataclass(frozen=True)
class SchedulePolicy:
    schedule: DrugSchedule
    dispensable_online: bool
    requires_prescription: bool
    requires_pharmacist_verification: bool
    requires_register_entry: bool
    notes: str


POLICY: dict[DrugSchedule, SchedulePolicy] = {
    DrugSchedule.OTC: SchedulePolicy(
        schedule=DrugSchedule.OTC,
        dispensable_online=True,
        requires_prescription=False,
        requires_pharmacist_verification=False,
        requires_register_entry=False,
        notes="General/OTC medicines. Ordered directly; drives daily volume.",
    ),
    DrugSchedule.H: SchedulePolicy(
        schedule=DrugSchedule.H,
        dispensable_online=True,
        requires_prescription=True,
        requires_pharmacist_verification=True,
        requires_register_entry=False,
        notes="Prescription upload plus pharmacist check before dispatch.",
    ),
    DrugSchedule.H1: SchedulePolicy(
        schedule=DrugSchedule.H1,
        dispensable_online=True,
        requires_prescription=True,
        requires_pharmacist_verification=True,
        requires_register_entry=True,
        notes=(
            "Includes many antibiotics and some sleeping/anti-anxiety drugs. "
            "Prescription plus pharmacist check plus a register entry recording "
            "prescriber, patient and quantity."
        ),
    ),
    DrugSchedule.X: SchedulePolicy(
        schedule=DrugSchedule.X,
        dispensable_online=False,
        requires_prescription=True,
        requires_pharmacist_verification=True,
        requires_register_entry=True,
        notes=(
            "Certain strong psychotropics and narcotics. Excluded from the platform: "
            "we believe these require the original paper prescription to be retained "
            "by the dispensing pharmacy. Confirm with counsel before ever enabling."
        ),
    ),
}


class NotDispensableOnline(Exception):
    """Raised when an order contains a medicine this platform must not dispense."""

    def __init__(self, medicine_name: str, schedule: DrugSchedule) -> None:
        self.medicine_name = medicine_name
        self.schedule = schedule
        super().__init__(
            f"{medicine_name} is Schedule {schedule} and cannot be dispensed through "
            f"this platform: {POLICY[schedule].notes}"
        )


def policy_for(schedule: DrugSchedule | str) -> SchedulePolicy:
    """Return the policy for a schedule, failing closed on anything unrecognised."""
    try:
        key = DrugSchedule(schedule)
    except ValueError as exc:
        raise NotDispensableOnline(f"<unclassified: {schedule!r}>", DrugSchedule.X) from exc
    return POLICY[key]


@dataclass(frozen=True)
class BasketControls:
    """The combined controls a basket of medicines attracts."""

    requires_prescription: bool
    requires_pharmacist_verification: bool
    register_entry_schedules: tuple[DrugSchedule, ...]

    @property
    def requires_register_entry(self) -> bool:
        return bool(self.register_entry_schedules)


def controls_for_basket(items: list[tuple[str, DrugSchedule | str]]) -> BasketControls:
    """Combine per-item policy into the controls for a whole order.

    Takes (medicine name, schedule) pairs so that a refusal can name the
    medicine responsible -- the patient needs to know which item to collect in
    person, not that "a basket item" was refused.

    Controls are a union, never an intersection: one Schedule H1 item in an
    otherwise OTC basket puts the whole order through verification.
    """
    requires_rx = False
    requires_verification = False
    register_schedules: list[DrugSchedule] = []

    for name, schedule in items:
        policy = policy_for(schedule)
        if not policy.dispensable_online:
            raise NotDispensableOnline(name, policy.schedule)
        requires_rx = requires_rx or policy.requires_prescription
        requires_verification = requires_verification or policy.requires_pharmacist_verification
        if policy.requires_register_entry:
            register_schedules.append(policy.schedule)

    return BasketControls(
        requires_prescription=requires_rx,
        requires_pharmacist_verification=requires_verification,
        register_entry_schedules=tuple(register_schedules),
    )
