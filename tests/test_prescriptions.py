"""Prescription verification rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medic.config import Settings
from medic.domain.prescriptions import (
    Authorisation,
    PrescriptionProblem,
    PrescriptionView,
    RequestedLine,
    check_prescription_covers,
    image_fingerprint,
)
from medic.domain.schedules import DrugSchedule

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def view(**kwargs):
    defaults = dict(
        id=1,
        customer_id=7,
        is_verified=True,
        issued_on=NOW - timedelta(days=5),
        max_uses=3,
        uses_count=0,
        authorisations=(Authorisation(medicine_id=1, authorised_qty=2),),
    )
    defaults.update(kwargs)
    return PrescriptionView(**defaults)


def controlled(qty=1, medicine_id=1):
    return [RequestedLine(medicine_id, "Azithromycin 500mg", qty, DrugSchedule.H1)]


def check(lines, prescription, *, customer_id=7, now=NOW):
    check_prescription_covers(
        lines, prescription, customer_id=customer_id, now=now, config=Settings()
    )


def test_otc_lines_need_no_prescription():
    check([RequestedLine(9, "Paracetamol", 1, DrugSchedule.OTC)], None)


def test_a_controlled_line_without_a_prescription_is_refused():
    with pytest.raises(PrescriptionProblem, match="prescription is required"):
        check(controlled(), None)


def test_an_unverified_prescription_cannot_release_a_scheduled_drug():
    with pytest.raises(PrescriptionProblem, match="not been verified"):
        check(controlled(), view(is_verified=False))


def test_another_customers_prescription_is_refused():
    with pytest.raises(PrescriptionProblem, match="different customer"):
        check(controlled(), view(customer_id=99))


def test_quantity_cannot_exceed_what_was_authorised():
    with pytest.raises(PrescriptionProblem, match="only 2 remaining"):
        check(controlled(qty=3), view())


def test_already_dispensed_quantity_counts_against_the_authorisation():
    partly_used = view(authorisations=(Authorisation(1, authorised_qty=2, dispensed_qty=2),))
    with pytest.raises(PrescriptionProblem, match="only 0 remaining"):
        check(controlled(qty=1), partly_used)


def test_a_medicine_not_on_the_prescription_is_refused():
    with pytest.raises(PrescriptionProblem, match="does not cover"):
        check(controlled(medicine_id=42), view())


def test_a_prescription_cannot_be_used_past_its_use_limit():
    """The brief's safeguard against one prescription backing endless refills."""
    with pytest.raises(PrescriptionProblem, match="already been used"):
        check(controlled(), view(max_uses=3, uses_count=3))


def test_an_expired_prescription_is_refused():
    config = Settings()
    stale = view(issued_on=NOW - timedelta(days=config.prescription_validity_days + 1))
    with pytest.raises(PrescriptionProblem, match="beyond the"):
        check(controlled(), stale)


def test_a_prescription_with_no_issue_date_is_not_aged_out():
    """We cannot age what we cannot date; the pharmacist is the backstop here."""
    check(controlled(), view(issued_on=None))


def test_a_mixed_basket_passes_on_its_scheduled_lines():
    lines = controlled() + [RequestedLine(9, "Paracetamol", 2, DrugSchedule.OTC)]
    check(lines, view())


def test_identical_images_share_a_fingerprint():
    assert image_fingerprint(b"scan-bytes") == image_fingerprint(b"scan-bytes")


def test_different_images_do_not_collide():
    assert image_fingerprint(b"scan-a") != image_fingerprint(b"scan-b")
