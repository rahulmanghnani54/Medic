"""End-to-end lifecycle through the service layer, against a real database."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from medic.domain.schedules import DrugSchedule, NotDispensableOnline
from medic.models import (
    AuditEvent,
    ChemistStock,
    GateState,
    Medicine,
    Order,
    OrderStatus,
    PrescriptionStatus,
    RegisterEntry,
)
from medic.services import ordering
from medic.services.ordering import LineRequest, OrderingError

MALVIYA = (26.8505, 75.8060)


def medicine(session, name) -> Medicine:
    found = session.scalars(select(Medicine).where(Medicine.name == name)).first()
    assert found is not None, f"{name} missing from seed data"
    return found


def place(session, lines, *, customer_id=1, prescription_id=None):
    return ordering.place_order(
        session,
        customer_id=customer_id,
        lines=lines,
        delivery_zone="Malviya Nagar",
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
        prescription_id=prescription_id,
    )


def upload(session, customer_id=1, payload=b"prescription-scan"):
    return ordering.store_prescription(
        session,
        customer_id=customer_id,
        image_bytes=payload,
        filename="rx.jpg",
        prescriber_name="Dr A Sharma",
        prescriber_reg_no="RMC-44120",
    )


# --- OTC happy path -------------------------------------------------------


def test_an_otc_order_runs_without_a_prescription(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    assert order.status is OrderStatus.AWAITING_FULFILMENT
    assert order.rx_gate is GateState.NOT_REQUIRED
    assert order.chemist_gate is GateState.PENDING
    assert order.chemist_id is not None


def test_accepting_an_otc_order_makes_it_ready(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    assert order.status is OrderStatus.READY_FOR_PICKUP


def test_the_full_otc_journey_ends_delivered_and_on_time(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    ordering.deliver(seeded, order_id=order.id)
    assert order.status is OrderStatus.DELIVERED
    assert order.promise_honoured is True


# --- Prescription path ----------------------------------------------------


def test_a_scheduled_drug_without_a_prescription_is_refused(seeded):
    with pytest.raises(OrderingError, match="prescription is required"):
        place(seeded, [LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)])


def test_a_scheduled_order_waits_at_the_verification_gate(seeded):
    prescription = upload(seeded)
    order = place(
        seeded,
        [LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)],
        prescription_id=prescription.id,
    )
    assert order.rx_gate is GateState.PENDING
    assert order.status is OrderStatus.AWAITING_FULFILMENT


def test_an_accepted_order_still_waits_for_the_pharmacist(seeded):
    """The chemist gate alone does not release a scheduled drug."""
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    assert order.chemist_gate is GateState.CLEARED
    assert order.status is OrderStatus.AWAITING_FULFILMENT


def test_verification_releases_a_waiting_order(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],
        pharmacist="Ph. R Nair",
    )
    assert order.rx_gate is GateState.CLEARED
    assert order.status is OrderStatus.READY_FOR_PICKUP


def test_a_rejected_prescription_closes_the_order(seeded):
    prescription = upload(seeded)
    order = place(
        seeded,
        [LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)],
        prescription_id=prescription.id,
    )
    ordering.reject_prescription(
        seeded,
        prescription_id=prescription.id,
        reason="prescriber registration number is not legible",
        pharmacist="Ph. R Nair",
    )
    assert order.status is OrderStatus.REJECTED_PRESCRIPTION
    assert order.rx_gate is GateState.FAILED
    assert "not legible" in order.closed_reason


def test_an_unverified_order_cannot_be_dispatched(seeded):
    prescription = upload(seeded)
    order = place(
        seeded,
        [LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)],
        prescription_id=prescription.id,
    )
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    with pytest.raises(Exception):  # noqa: B017 - IllegalTransition or OrderingError
        ordering.dispatch(seeded, order_id=order.id, rider="rider-1")


def test_dispensing_more_than_authorised_is_refused(seeded):
    """Verification authorises a quantity, and dispatch must not exceed it."""
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 2)], prescription_id=prescription.id)
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],  # pharmacist allows only one strip
        pharmacist="Ph. R Nair",
    )
    with pytest.raises(OrderingError, match="would exceed"):
        ordering.dispatch(seeded, order_id=order.id, rider="rider-1")


def test_an_already_verified_prescription_must_cover_the_basket_at_checkout(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],
        pharmacist="Ph. R Nair",
    )
    with pytest.raises(OrderingError, match="only 1 remaining"):
        place(seeded, [LineRequest(azithro.id, 2)], prescription_id=prescription.id)


def test_a_verified_prescription_clears_the_gate_at_checkout(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],
        pharmacist="Ph. R Nair",
    )
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    assert order.rx_gate is GateState.CLEARED


def test_another_customers_prescription_cannot_be_attached(seeded):
    prescription = upload(seeded, customer_id=2)
    with pytest.raises(OrderingError, match="different customer"):
        place(
            seeded,
            [LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)],
            customer_id=1,
            prescription_id=prescription.id,
        )


def test_a_reuploaded_prescription_is_flagged_for_the_pharmacist(seeded):
    first = upload(seeded, payload=b"same-image-bytes")
    second = upload(seeded, payload=b"same-image-bytes")
    assert first.duplicate_of_id is None
    assert second.duplicate_of_id == first.id


def test_a_different_image_is_not_flagged(seeded):
    upload(seeded, payload=b"image-one")
    other = upload(seeded, payload=b"image-two")
    assert other.duplicate_of_id is None


def test_a_prescription_cannot_be_verified_twice(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],
        pharmacist="Ph. R Nair",
    )
    with pytest.raises(OrderingError, match="already"):
        ordering.verify_prescription(
            seeded,
            prescription_id=prescription.id,
            authorisations=[(azithro.id, 1)],
            pharmacist="Ph. Someone Else",
        )


def test_a_rejection_needs_a_reason(seeded):
    prescription = upload(seeded)
    with pytest.raises(OrderingError, match="reason"):
        ordering.reject_prescription(
            seeded, prescription_id=prescription.id, reason="  ", pharmacist="Ph. R Nair"
        )


# --- Schedule X refusal ---------------------------------------------------


def test_a_schedule_x_drug_cannot_be_ordered_at_all(seeded):
    alprazolam = medicine(seeded, "Alprazolam 0.5mg")
    assert alprazolam.schedule == DrugSchedule.X
    with pytest.raises(NotDispensableOnline):
        place(seeded, [LineRequest(alprazolam.id, 1)])


def test_a_schedule_x_drug_poisons_an_otherwise_fine_basket(seeded):
    with pytest.raises(NotDispensableOnline):
        place(
            seeded,
            [
                LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1),
                LineRequest(medicine(seeded, "Alprazolam 0.5mg").id, 1),
            ],
        )


# --- Schedule H1 register -------------------------------------------------


def test_dispatching_an_h1_order_writes_a_register_entry(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 1)],
        pharmacist="Ph. R Nair",
    )
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")

    entries = seeded.scalars(
        select(RegisterEntry).where(RegisterEntry.order_id == order.id)
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.medicine_name == "Azithromycin 500mg"
    assert entry.schedule == DrugSchedule.H1
    assert entry.prescriber_reg_no == "RMC-44120"
    assert entry.patient_name
    assert entry.pharmacist_reg_no


def test_an_otc_order_writes_no_register_entry(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    assert seeded.scalars(select(RegisterEntry)).all() == []


def test_dispatch_increments_the_prescriptions_use_count(seeded):
    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro.id, 3)],
        pharmacist="Ph. R Nair",
    )
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    assert prescription.uses_count == 1
    assert prescription.items[0].dispensed_qty == 1


# --- Stock handling -------------------------------------------------------


def test_accepting_an_order_reserves_the_stock(seeded):
    paracetamol = medicine(seeded, "Paracetamol 650mg")
    order = place(seeded, [LineRequest(paracetamol.id, 2)])
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == order.chemist_id,
            ChemistStock.medicine_id == paracetamol.id,
        )
    ).one()
    before = row.qty_on_hand
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    assert row.qty_on_hand == before - 2


def test_rejecting_after_acceptance_returns_the_stock(seeded):
    paracetamol = medicine(seeded, "Paracetamol 650mg")
    order = place(seeded, [LineRequest(paracetamol.id, 2)])
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == order.chemist_id,
            ChemistStock.medicine_id == paracetamol.id,
        )
    ).one()
    before = row.qty_on_hand
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.chemist_reject(
        seeded,
        order_id=order.id,
        chemist_id=order.chemist_id,
        reason="strip damaged on inspection",
    )
    assert row.qty_on_hand == before


def test_a_stock_out_rejection_closes_the_order(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_reject(
        seeded, order_id=order.id, chemist_id=order.chemist_id, reason="not in stock"
    )
    assert order.status is OrderStatus.CANCELLED_STOCK_OUT


def test_a_chemist_cannot_act_on_another_chemists_order(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    other = 1 if order.chemist_id != 1 else 2
    with pytest.raises(OrderingError, match="not offered to you"):
        ordering.chemist_accept(seeded, order_id=order.id, chemist_id=other)


def test_an_order_cannot_be_accepted_twice(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    with pytest.raises(OrderingError):
        ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)


def test_no_chemist_in_range_is_a_clear_refusal(seeded):
    """Matching must refuse rather than promise 60 minutes it cannot keep."""
    with pytest.raises(OrderingError, match="60-minute window"):
        ordering.place_order(
            seeded,
            customer_id=1,
            lines=[LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)],
            delivery_zone="Jhotwara",
            delivery_lat=27.2000,  # well outside the pilot zones
            delivery_lng=76.2000,
        )


def test_a_cold_chain_item_goes_to_a_cold_chain_chemist(seeded):
    from medic.models import Chemist

    prescription = upload(seeded)
    insulin = medicine(seeded, "Insulin Glargine 100IU")
    order = place(seeded, [LineRequest(insulin.id, 1)], prescription_id=prescription.id)
    chemist = seeded.get(Chemist, order.chemist_id)
    assert chemist.can_cold_chain


# --- Cancellation ---------------------------------------------------------


def test_a_customer_can_cancel_before_pickup(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.cancel_by_customer(
        seeded, order_id=order.id, customer_id=1, reason="ordered by mistake"
    )
    assert order.status is OrderStatus.CANCELLED_BY_CUSTOMER


def test_a_customer_cannot_cancel_once_the_rider_has_collected(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    with pytest.raises(OrderingError, match="rider already holds"):
        ordering.cancel_by_customer(
            seeded, order_id=order.id, customer_id=1, reason="changed my mind"
        )


def test_a_customer_cannot_cancel_someone_elses_order(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    with pytest.raises(OrderingError, match="not your order"):
        ordering.cancel_by_customer(seeded, order_id=order.id, customer_id=2, reason="nope")


# --- Audit and economics --------------------------------------------------


def test_every_step_leaves_an_audit_trail(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    ordering.deliver(seeded, order_id=order.id)

    actions = [
        event.action
        for event in seeded.scalars(
            select(AuditEvent).where(AuditEvent.subject_id == order.id).order_by(AuditEvent.id)
        )
    ]
    assert actions == [
        "order.placed",
        "order.accepted",
        "order.ready_for_pickup",
        "order.dispatched",
        "order.delivered",
    ]


def test_economics_reports_nothing_before_the_first_delivery(seeded):
    report = ordering.economics(seeded)
    assert report.delivered_orders == 0
    assert report.break_even_orders_per_month is None


def test_economics_measures_delivered_orders(seeded):
    thyroxine = medicine(seeded, "Thyroxine 50mcg")
    order = place(seeded, [LineRequest(thyroxine.id, 4)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    ordering.deliver(seeded, order_id=order.id)

    report = ordering.economics(seeded)
    assert report.delivered_orders == 1
    assert report.on_time_rate == 1.0
    assert report.mean_order_value_paise == order.total_paise


def test_chemist_sla_counters_track_offers_and_outcomes(seeded):
    from medic.models import Chemist

    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    chemist = seeded.get(Chemist, order.chemist_id)
    assert chemist.orders_offered == 1
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    ordering.deliver(seeded, order_id=order.id)
    assert chemist.orders_accepted == 1
    assert chemist.orders_completed == 1
    assert chemist.orders_on_time == 1


# --- Quoting --------------------------------------------------------------


def test_a_quote_does_not_create_an_order(seeded):
    quote = ordering.quote_order(
        seeded,
        customer_id=1,
        lines=[LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)],
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
    )
    assert quote.eta.total_minutes > 0
    assert seeded.scalars(select(Order)).all() == []


def test_a_quote_announces_the_verification_step(seeded):
    quote = ordering.quote_order(
        seeded,
        customer_id=1,
        lines=[LineRequest(medicine(seeded, "Azithromycin 500mg").id, 1)],
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
    )
    assert quote.requires_prescription
    assert quote.requires_verification


def test_an_unknown_medicine_is_refused(seeded):
    with pytest.raises(OrderingError, match="unknown medicine"):
        place(seeded, [LineRequest(999_999, 1)])


def test_an_empty_basket_is_refused(seeded):
    with pytest.raises(OrderingError, match="empty"):
        place(seeded, [])


# --- Regression guards ----------------------------------------------------


def test_a_chemist_can_withdraw_after_accepting_and_before_pickup(seeded):
    """Declared stock can be wrong; the chemist must be able to pull out.

    An OTC order clears both gates on acceptance and goes straight to
    READY_FOR_PICKUP, so withdrawal has to be allowed from that state too.
    """
    paracetamol = medicine(seeded, "Paracetamol 650mg")
    order = place(seeded, [LineRequest(paracetamol.id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    assert order.status is OrderStatus.READY_FOR_PICKUP
    ordering.chemist_reject(
        seeded,
        order_id=order.id,
        chemist_id=order.chemist_id,
        reason="strip found short-dated on the shelf",
    )
    assert order.status is OrderStatus.CANCELLED_STOCK_OUT


def test_a_chemist_cannot_withdraw_once_the_rider_has_collected(seeded):
    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    ordering.chemist_accept(seeded, order_id=order.id, chemist_id=order.chemist_id)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    with pytest.raises(OrderingError, match="cannot be rejected"):
        ordering.chemist_reject(
            seeded, order_id=order.id, chemist_id=order.chemist_id, reason="too late"
        )


def test_timestamps_survive_the_database_round_trip_as_utc_aware(seeded):
    """SQLite drops tzinfo, which would break the SLA clock. See UtcDateTime.

    A naive timestamp read back here would make every sla_view() call raise, so
    this guards the whole 60-minute promise.
    """
    from datetime import UTC

    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    seeded.commit()
    seeded.expire_all()
    reloaded = seeded.get(Order, order.id)
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at.utcoffset().total_seconds() == 0
    # The property that depends on it must still work.
    assert reloaded.deadline > reloaded.created_at
    from medic.domain.orders import sla_view

    sla = sla_view(reloaded.created_at, reloaded.promised_minutes, datetime.now(UTC))
    assert 0 < sla.minutes_remaining <= 60


def test_the_price_cap_is_applied_to_a_real_catalogue_item(seeded):
    """Paracetamol is seeded with an MRP above its DPCO ceiling."""
    paracetamol = medicine(seeded, "Paracetamol 650mg")
    assert paracetamol.dpco_ceiling_paise < paracetamol.mrp_paise
    quote = ordering.quote_order(
        seeded,
        customer_id=1,
        lines=[LineRequest(paracetamol.id, 1)],
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
    )
    assert quote.lines[0].unit_price_paise == paracetamol.dpco_ceiling_paise
    assert quote.totals.statutory_reduction_paise > 0
    assert quote.totals.chemist_funded_discount_paise == 0


def test_enum_columns_come_back_as_enum_members_not_strings(seeded):
    """Guards identity comparisons against a database round trip.

    `order.status is OrderStatus.X` is used throughout the service layer. If the
    column hands back a bare string those checks quietly evaluate False, which
    reads as "nothing is blocking this order" instead of raising. See
    models.enum_column.
    """
    from medic.models import GateState

    prescription = upload(seeded)
    azithro = medicine(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro.id, 1)], prescription_id=prescription.id)
    seeded.commit()
    seeded.expire_all()

    reloaded = seeded.get(Order, order.id)
    assert reloaded.status is OrderStatus.AWAITING_FULFILMENT
    assert reloaded.chemist_gate is GateState.PENDING
    assert reloaded.rx_gate is GateState.PENDING
    assert reloaded.items[0].schedule is DrugSchedule.H1
    assert seeded.get(Medicine, azithro.id).schedule is DrugSchedule.H1
    assert reloaded.status is not OrderStatus.DELIVERED

    from medic.models import Prescription

    assert seeded.get(Prescription, prescription.id).status is PrescriptionStatus.PENDING


def test_a_reloaded_order_still_reports_what_it_waits_on(seeded):
    """The user-visible consequence of the enum round-trip bug."""
    from medic.api.presenters import waiting_on

    order = place(seeded, [LineRequest(medicine(seeded, "Paracetamol 650mg").id, 1)])
    seeded.commit()
    seeded.expire_all()
    assert waiting_on(seeded.get(Order, order.id)) == "chemist to confirm stock"
