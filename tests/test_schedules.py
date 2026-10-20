"""Drug schedule policy: the gate every other rule depends on."""

from __future__ import annotations

import pytest

from medic.domain.schedules import (
    POLICY,
    DrugSchedule,
    NotDispensableOnline,
    controls_for_basket,
    policy_for,
)


def test_otc_needs_no_prescription():
    policy = policy_for(DrugSchedule.OTC)
    assert policy.dispensable_online
    assert not policy.requires_prescription
    assert not policy.requires_register_entry


def test_schedule_h_needs_prescription_but_no_register():
    policy = policy_for(DrugSchedule.H)
    assert policy.requires_prescription
    assert policy.requires_pharmacist_verification
    assert not policy.requires_register_entry


def test_schedule_h1_needs_a_register_entry():
    policy = policy_for(DrugSchedule.H1)
    assert policy.requires_prescription
    assert policy.requires_register_entry


def test_schedule_x_is_never_dispensable():
    assert not POLICY[DrugSchedule.X].dispensable_online


def test_unknown_schedule_fails_closed():
    """An unclassified medicine must be refused, not waved through as OTC."""
    with pytest.raises(NotDispensableOnline):
        policy_for("SCHEDULE-Q")


def test_basket_controls_are_a_union_not_an_intersection():
    controls = controls_for_basket(
        [
            ("Paracetamol", DrugSchedule.OTC),
            ("ORS", DrugSchedule.OTC),
            ("Azithromycin", DrugSchedule.H1),
        ]
    )
    assert controls.requires_prescription
    assert controls.requires_pharmacist_verification
    assert controls.register_entry_schedules == (DrugSchedule.H1,)


def test_one_schedule_x_item_poisons_the_whole_basket():
    with pytest.raises(NotDispensableOnline, match="Alprazolam"):
        controls_for_basket(
            [("Paracetamol", DrugSchedule.OTC), ("Alprazolam", DrugSchedule.X)]
        )


def test_a_refusal_names_the_medicine_responsible():
    """The patient has to know which item to collect in person."""
    with pytest.raises(NotDispensableOnline) as raised:
        controls_for_basket([("Alprazolam 0.5mg", DrugSchedule.X)])
    assert raised.value.medicine_name == "Alprazolam 0.5mg"
    assert "Alprazolam 0.5mg" in str(raised.value)


def test_otc_only_basket_attracts_no_controls():
    controls = controls_for_basket([("Paracetamol", DrugSchedule.OTC)])
    assert not controls.requires_prescription
    assert not controls.requires_register_entry
