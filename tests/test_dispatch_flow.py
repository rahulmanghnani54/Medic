"""The Uber-style dispatch loop, end to end against a real database.

The single most important property under test is the ASYMMETRY Uber's
dispatch has and the previous design did not: a chemist refusing an order is
a routing event, not the death of the order. Everything else here follows
from that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from medic.domain.dispatch import OfferState
from medic.models import (
    ChemistStock,
    GateState,
    Medicine,
    Offer,
    OrderStatus,
    RegisterEntry,
    StockLease,
)
from medic.services import dispatching, ordering, stock
from medic.services.dispatching import DispatchError
from medic.services.ordering import LineRequest, OrderingError

#: Noon IST, so every seeded chemist is inside its opening hours. Matching
#: takes `at` precisely so this is controllable; a wall-clock test would pass
#: or fail depending on the hour it ran.
NOON = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)
MALVIYA = (26.8505, 75.8060)


def mid(session, name: str) -> int:
    return session.scalars(select(Medicine).where(Medicine.name == name)).one().id


def place(session, lines, *, customer_id=1, prescription_id=None, now=NOON):
    return ordering.place_order(
        session,
        customer_id=customer_id,
        lines=lines,
        delivery_zone="Malviya Nagar",
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
        prescription_id=prescription_id,
        now=now,
    )


def otc(session, qty=1):
    return [LineRequest(mid(session, "Paracetamol 650mg"), qty)]


def upload(session, customer_id=1, payload=b"scan"):
    return ordering.store_prescription(
        session,
        customer_id=customer_id,
        image_bytes=payload,
        filename="rx.jpg",
        prescriber_name="Dr A Sharma",
        prescriber_reg_no="RMC-44120",
    )


# --- No assignment at checkout --------------------------------------------


def test_an_order_enters_sourcing_with_no_chemist_bound(seeded):
    """The core change: the request no longer carries an assignment."""
    order = place(seeded, otc(seeded))
    assert order.status is OrderStatus.SEEKING_CHEMIST
    assert order.chemist_id is None


def test_placing_an_order_immediately_makes_the_first_offer(seeded):
    """The 60-minute clock is already running, so do not wait for a worker tick."""
    order = place(seeded, otc(seeded))
    offers = dispatching.offers_for(seeded, order.id)
    assert len(offers) == 1
    assert offers[0].state is OfferState.OFFERED
    assert order.chemist_gate is GateState.PENDING


def test_the_offer_carries_its_own_committed_expiry(seeded):
    """The timer is written with the state change, so a crash cannot lose it."""
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    assert offer.expires_at > offer.offered_at
    assert offer.eta_minutes > 0


def test_a_quote_names_no_chemist_and_gives_a_band(seeded):
    """Nobody has agreed to anything yet, so there is no promise to state."""
    verdict = ordering.assess_feasibility(
        seeded,
        lines=otc(seeded),
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
        now=NOON,
    )
    assert verdict.feasible
    assert verdict.candidate_count >= 1
    assert verdict.eta_minutes_low <= verdict.eta_minutes_high
    assert not hasattr(verdict, "chemist_id")


# --- The asymmetry --------------------------------------------------------


def test_a_decline_does_not_kill_the_order(seeded):
    """Uber's asymmetry: supply refusing is a routing event, not a failure."""
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="too busy", now=NOON
    )
    assert order.status is OrderStatus.SEEKING_CHEMIST
    assert order.chemist_gate is GateState.SEEKING


def test_a_decline_is_followed_by_an_offer_to_the_next_candidate(seeded):
    order = place(seeded, otc(seeded))
    first = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=first.id, chemist_id=first.chemist_id, reason="busy", now=NOON
    )
    dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    second = dispatching.open_offer(seeded, order.id)
    assert second is not None
    assert second.chemist_id != first.chemist_id
    assert second.attempt_seq == first.attempt_seq + 1


def test_an_unanswered_offer_expires_and_frees_the_order(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    affected = dispatching.expire_due_offers(seeded, now=offer.expires_at + timedelta(seconds=1))
    assert affected == [order.id]
    assert offer.state is OfferState.EXPIRED
    assert order.chemist_gate is GateState.SEEKING


def test_a_late_accept_loses_the_window_it_missed(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    with pytest.raises(DispatchError, match="expired"):
        dispatching.accept_offer(
            seeded,
            offer_id=offer.id,
            chemist_id=offer.chemist_id,
            now=offer.expires_at + timedelta(minutes=1),
        )
    assert offer.state is OfferState.EXPIRED


def test_only_one_offer_is_open_at_a_time(seeded):
    """Stock is depletable, so broadcasting would double-promise units."""
    order = place(seeded, otc(seeded))
    dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    open_offers = [
        o for o in dispatching.offers_for(seeded, order.id) if o.state is OfferState.OFFERED
    ]
    assert len(open_offers) == 1


def test_advance_sourcing_is_idempotent_while_an_offer_is_open(seeded):
    order = place(seeded, otc(seeded))
    outcome = dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    assert outcome.action == "waiting"


def test_a_chemist_cannot_answer_an_offer_made_to_someone_else(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    other = 2 if offer.chemist_id != 2 else 3
    with pytest.raises(DispatchError, match="not made to you"):
        dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=other, now=NOON)


# --- Accept -------------------------------------------------------------


def test_accepting_binds_the_chemist_and_commits_a_firm_eta(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(
        seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON
    )
    assert order.status is OrderStatus.READY_FOR_PICKUP  # OTC: both gates clear
    assert order.chemist_id == offer.chemist_id
    assert order.committed_eta_minutes is not None
    assert offer.state is OfferState.ACCEPTED


def test_accepting_leases_stock_rather_than_decrementing_it(seeded):
    """on_hand stays shelf truth; a lease only makes units unavailable."""
    order = place(seeded, otc(seeded, qty=2))
    offer = dispatching.open_offer(seeded, order.id)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    on_hand = row.qty_on_hand
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    assert row.qty_on_hand == on_hand
    assert row.qty_reserved == 2
    assert row.qty_available == on_hand - 2


def test_leased_units_are_not_offered_to_another_order(seeded):
    """Availability, not on-hand, is what the matcher sees."""
    paracetamol = mid(seeded, "Paracetamol 650mg")
    first = place(seeded, [LineRequest(paracetamol, 2)])
    offer = dispatching.open_offer(seeded, first.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    from medic.services import supply

    candidate = next(
        c
        for c in supply.candidates_near(seeded, lat=MALVIYA[0], lng=MALVIYA[1])
        if c.chemist_id == offer.chemist_id
    )
    assert candidate.stock[paracetamol] == 18  # seeded 20, two leased


def test_a_second_accept_of_the_same_offer_is_refused(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    with pytest.raises(DispatchError, match="already"):
        dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)


# --- Withdrawal after accept ---------------------------------------------


def test_a_chemist_who_accepted_can_hand_the_order_back(seeded):
    """An accept is a claim about declared stock; shelf truth comes later.

    This outcome has no ride-hailing analogue, and the previous design killed
    the order for it.
    """
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    dispatching.withdraw_after_accept(
        seeded, order_id=order.id, chemist_id=offer.chemist_id, reason="strip missing", now=NOON
    )
    assert order.status is OrderStatus.SEEKING_CHEMIST
    assert order.chemist_id is None
    assert order.committed_eta_minutes is None
    assert offer.state is OfferState.WITHDRAWN


def test_withdrawing_returns_the_leased_units(seeded):
    order = place(seeded, otc(seeded, qty=2))
    offer = dispatching.open_offer(seeded, order.id)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    dispatching.withdraw_after_accept(
        seeded, order_id=order.id, chemist_id=offer.chemist_id, reason="gone", now=NOON
    )
    assert row.qty_reserved == 0


def test_goods_already_dispensed_cannot_be_withdrawn(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    with pytest.raises(DispatchError, match="no longer be handed back"):
        dispatching.withdraw_after_accept(
            seeded, order_id=order.id, chemist_id=offer.chemist_id, reason="late", now=NOON
        )


def test_the_custody_check_catches_a_dispense_whose_status_did_not_advance(seeded):
    """Defence in depth behind the status guard.

    Status is the first guard, but once transport events arrive
    asynchronously it stops being a sound proxy for where the goods are. The
    dispensing record is the fact that matters, so withdrawal consults it too.
    """
    from medic.services import dispensing

    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    # Dispense without advancing the status, as a crash between the two would.
    dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")
    assert order.status is OrderStatus.READY_FOR_PICKUP
    with pytest.raises(DispatchError, match="custody"):
        dispatching.withdraw_after_accept(
            seeded, order_id=order.id, chemist_id=offer.chemist_id, reason="late", now=NOON
        )


# --- Exhaustion -----------------------------------------------------------


def test_running_out_of_candidates_refuses_honestly(seeded):
    """Invariant: when supply cannot keep the promise we refuse, not over-promise."""
    order = place(seeded, otc(seeded))
    for _ in range(8):
        dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
        offer = dispatching.open_offer(seeded, order.id)
        if offer is None:
            break
        dispatching.decline_offer(
            seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="no", now=NOON
        )
    assert order.status is OrderStatus.CANCELLED_NO_CHEMIST
    assert order.closed_reason
    assert order.chemist_id is None


def test_exhaustion_releases_every_lease(seeded):
    order = place(seeded, otc(seeded))
    for _ in range(8):
        dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
        offer = dispatching.open_offer(seeded, order.id)
        if offer is None:
            break
        dispatching.decline_offer(
            seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="no", now=NOON
        )
    held = [
        lease
        for lease in seeded.scalars(select(StockLease).where(StockLease.order_id == order.id))
        if lease.state == "HELD"
    ]
    assert held == []


def test_the_budget_shrinks_so_a_far_chemist_stops_being_feasible(seeded):
    """A candidate feasible at minute 0 is not feasible after a timed-out round.

    The previous design checked feasibility once, against the full hour.
    """
    order = place(seeded, otc(seeded))
    first = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=first.id, chemist_id=first.chemist_id, reason="busy", now=NOON
    )
    dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    near_in_time = dispatching.open_offer(seeded, order.id)
    assert near_in_time is not None

    # Same order, same candidates, but most of the hour is gone.
    late = NOON + timedelta(minutes=50)
    dispatching.expire_due_offers(seeded, now=late)
    outcome = dispatching.advance_sourcing(seeded, order_id=order.id, now=late)
    assert outcome.action == "exhausted"
    assert order.status is OrderStatus.CANCELLED_NO_CHEMIST


def test_the_sourcing_loop_is_bounded_by_configuration(seeded):
    order = place(seeded, otc(seeded))
    for _ in range(10):
        dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
        offer = dispatching.open_offer(seeded, order.id)
        if offer is None:
            break
        dispatching.decline_offer(
            seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="no", now=NOON
        )
    from medic.config import settings

    assert order.attempt_seq <= settings.max_sourcing_attempts


# --- The prescription gate under re-sourcing -------------------------------


def test_a_prescription_rejected_mid_sourcing_closes_the_order(seeded):
    """The hole this rewrite had to close.

    An order still being offered when the pharmacist rejects must stop. The
    previous hook only looked at AWAITING_FULFILMENT, so the dispatcher would
    have kept offering, a chemist would have accepted, and stock for a
    scheduled drug would have been leased against a refused prescription.
    """
    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    assert order.status is OrderStatus.SEEKING_CHEMIST

    ordering.reject_prescription(
        seeded,
        prescription_id=prescription.id,
        reason="prescriber registration is not legible",
        pharmacist="Ph. R Nair",
    )
    assert order.status is OrderStatus.REJECTED_PRESCRIPTION
    assert order.rx_gate is GateState.FAILED
    assert order.chemist_id is None


def test_a_rejected_prescription_leaves_no_open_offer(seeded):
    prescription = upload(seeded)
    order = place(
        seeded,
        [LineRequest(mid(seeded, "Azithromycin 500mg"), 1)],
        prescription_id=prescription.id,
    )
    ordering.reject_prescription(
        seeded, prescription_id=prescription.id, reason="illegible", pharmacist="Ph. R Nair"
    )
    assert dispatching.open_offer(seeded, order.id) is None


def test_a_rejected_prescription_releases_leased_stock(seeded):
    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id, ChemistStock.medicine_id == azithro
        )
    ).one()
    assert row.qty_reserved == 1
    ordering.reject_prescription(
        seeded, prescription_id=prescription.id, reason="not valid", pharmacist="Ph. R Nair"
    )
    assert row.qty_reserved == 0


def test_the_dispatcher_refuses_to_offer_a_rejected_prescription(seeded):
    """Defence in depth: even if an order were left in sourcing, no offer goes out."""
    prescription = upload(seeded)
    order = place(
        seeded,
        [LineRequest(mid(seeded, "Azithromycin 500mg"), 1)],
        prescription_id=prescription.id,
    )
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="busy", now=NOON
    )
    from medic.models import PrescriptionStatus

    prescription.status = PrescriptionStatus.REJECTED  # simulate a lost hook
    seeded.flush()
    outcome = dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    assert outcome.action == "exhausted"
    assert "rejected" in outcome.reason


def test_verification_while_sourcing_does_not_make_an_order_ready(seeded):
    """Clearing one gate cannot promote an order that has no chemist."""
    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="busy", now=NOON
    )
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro, 1)],
        pharmacist="Ph. R Nair",
    )
    assert order.rx_gate is GateState.CLEARED
    # The invariant: an order can never be READY_FOR_PICKUP with no chemist
    # bound. Here the nudge to the dispatcher exhausts the candidates (the
    # flagship partner is the only one stocking this antibiotic nearby), so
    # the order closes honestly rather than sitting half-ready.
    assert order.status is not OrderStatus.READY_FOR_PICKUP
    assert order.chemist_id is None


# --- Dispensing idempotency -----------------------------------------------


def test_dispensing_twice_writes_one_register_entry(seeded):
    """At-least-once delivery makes a replay a question of when, not if."""
    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro, 1)],
        pharmacist="Ph. R Nair",
    )
    from medic.services import dispensing

    first = dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")
    second = dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")
    assert first.register_entries_written == 1
    assert second.already_done
    assert second.register_entries_written == 0
    rows = seeded.scalars(select(RegisterEntry).where(RegisterEntry.order_id == order.id)).all()
    assert len(rows) == 1


def test_a_replayed_dispense_does_not_double_debit_the_prescription(seeded):
    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    ordering.verify_prescription(
        seeded,
        prescription_id=prescription.id,
        authorisations=[(azithro, 3)],
        pharmacist="Ph. R Nair",
    )
    from medic.services import dispensing

    dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")
    dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")
    assert prescription.uses_count == 1
    assert prescription.items[0].dispensed_qty == 1


def test_dispensing_consumes_the_lease(seeded):
    order = place(seeded, otc(seeded, qty=2))
    offer = dispatching.open_offer(seeded, order.id)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    on_hand = row.qty_on_hand
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    assert row.qty_on_hand == on_hand - 2  # now it really has left the shelf
    assert row.qty_reserved == 0


def test_dispensing_refuses_while_a_gate_is_open(seeded):
    """The last interlock before goods leave the pharmacy."""
    from medic.services import dispensing

    prescription = upload(seeded)
    azithro = mid(seeded, "Azithromycin 500mg")
    order = place(seeded, [LineRequest(azithro, 1)], prescription_id=prescription.id)
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    assert order.rx_gate is GateState.PENDING
    with pytest.raises(dispensing.DispensingRefused, match="gates"):
        dispensing.dispense(seeded, order=order, courier_id=None, actor="admin")


# --- Stock lease safety ---------------------------------------------------


def test_releasing_a_lease_twice_does_not_inflate_stock(seeded):
    """A double release of a decrement would invent phantom units."""
    order = place(seeded, otc(seeded, qty=3))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    on_hand = row.qty_on_hand
    assert stock.release(seeded, order_id=order.id) == 1
    assert stock.release(seeded, order_id=order.id) == 0
    assert stock.release(seeded, order_id=order.id) == 0
    assert row.qty_on_hand == on_hand
    assert row.qty_reserved == 0


def test_holding_the_same_lease_twice_reserves_once(seeded):
    order = place(seeded, otc(seeded, qty=2))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    stock.hold(seeded, order=order, chemist_id=offer.chemist_id)
    assert row.qty_reserved == 2


def test_a_chemist_without_free_units_cannot_accept(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    row.qty_reserved = row.qty_on_hand  # everything already promised elsewhere
    seeded.flush()
    with pytest.raises(stock.InsufficientStock):
        dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)


# --- Cancellation ---------------------------------------------------------


def test_a_customer_can_cancel_while_the_order_is_being_sourced(seeded):
    order = place(seeded, otc(seeded))
    ordering.cancel_by_customer(
        seeded, order_id=order.id, customer_id=1, reason="ordered by mistake"
    )
    assert order.status is OrderStatus.CANCELLED_BY_CUSTOMER
    assert dispatching.open_offer(seeded, order.id) is None


def test_cancelling_releases_leases_and_supersedes_the_offer(seeded):
    order = place(seeded, otc(seeded, qty=2))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    row = seeded.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == offer.chemist_id,
            ChemistStock.medicine_id == mid(seeded, "Paracetamol 650mg"),
        )
    ).one()
    ordering.cancel_by_customer(seeded, order_id=order.id, customer_id=1, reason="changed my mind")
    assert row.qty_reserved == 0


def test_a_customer_cannot_cancel_once_the_goods_have_been_dispensed(seeded):
    """Custody is read from the dispensing record, not the status column."""
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.accept_offer(seeded, offer_id=offer.id, chemist_id=offer.chemist_id, now=NOON)
    ordering.dispatch(seeded, order_id=order.id, rider="rider-1")
    with pytest.raises(OrderingError, match="already left the pharmacy|rider already holds"):
        ordering.cancel_by_customer(
            seeded, order_id=order.id, customer_id=1, reason="too late"
        )


# --- Audit ----------------------------------------------------------------


def test_the_offer_loop_is_fully_audited(seeded):
    order = place(seeded, otc(seeded))
    offer = dispatching.open_offer(seeded, order.id)
    dispatching.decline_offer(
        seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="busy", now=NOON
    )
    dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    from medic.models import AuditEvent

    actions = [
        e.action
        for e in seeded.scalars(select(AuditEvent).order_by(AuditEvent.id))
    ]
    assert "order.placed" in actions
    assert actions.count("offer.made") == 2
    assert "offer.declined" in actions


def test_every_offer_attempt_is_recorded_against_the_order(seeded):
    """The funnel is the diagnostic: who was asked, and what they said."""
    order = place(seeded, otc(seeded))
    for _ in range(3):
        offer = dispatching.open_offer(seeded, order.id)
        if offer is None:
            break
        dispatching.decline_offer(
            seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="busy", now=NOON
        )
        dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    offers = seeded.scalars(select(Offer).where(Offer.order_id == order.id)).all()
    assert len(offers) >= 2
    assert len({o.chemist_id for o in offers}) == len(offers)  # nobody asked twice


def test_an_order_is_never_offered_to_the_same_chemist_twice(seeded):
    order = place(seeded, otc(seeded))
    for _ in range(8):
        offer = dispatching.open_offer(seeded, order.id)
        if offer is None:
            break
        dispatching.decline_offer(
            seeded, offer_id=offer.id, chemist_id=offer.chemist_id, reason="busy", now=NOON
        )
        dispatching.advance_sourcing(seeded, order_id=order.id, now=NOON)
    ids = [o.chemist_id for o in dispatching.offers_for(seeded, order.id)]
    assert len(ids) == len(set(ids))


def test_orders_carry_the_clinical_urgency_of_their_most_urgent_line(seeded):
    """Dispatch sequencing, the lawful substitute for surge's rationing half."""
    order = place(
        seeded,
        [
            LineRequest(mid(seeded, "Paracetamol 650mg"), 1),
            LineRequest(mid(seeded, "Azithromycin 500mg"), 1),
        ],
        prescription_id=upload(seeded).id,
    )
    from medic.domain.triage import ClinicalUrgency

    assert order.urgency is ClinicalUrgency.TIME_SENSITIVE
