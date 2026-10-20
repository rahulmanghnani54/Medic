"""The courier supply side, end to end against a real database.

The courier leg is the part of this marketplace that behaves most like Uber's
driver side: go on shift, stream position, answer a short-windowed offer, work
an ordered plan of stops. What does not transfer is CUSTODY. Completing a
collection stop IS the dispensing event, so it writes the Schedule H1 register
and names the courier who took the package -- which is the whole reason a
courier carries a per-person token rather than a shared fleet secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from medic.domain.couriers import (
    CourierState,
    CustodyHeld,
    IllegalStopTransition,
    StopKind,
)
from medic.domain.dispatch import OfferState
from medic.models import Chemist, Courier, CourierPosition, Medicine, OrderStatus, RegisterEntry
from medic.services import couriers, dispatching, ordering
from medic.services.couriers import CourierError
from medic.services.ordering import LineRequest

#: Noon IST, so every seeded chemist is inside its opening hours. Matching
#: takes `now` precisely so this is controllable; a wall-clock test would pass
#: or fail depending on the hour it ran.
NOON = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)
MALVIYA = (26.8505, 75.8060)
#: Roughly 9 km from Malviya Nagar, so comfortably outside the pickup cap.
VAISHALI = (26.9124, 75.7400)

COLLECTED_AT = NOON + timedelta(minutes=9)
HANDED_OVER_AT = NOON + timedelta(minutes=24)


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


def online(session, *courier_ids: int, now=NOON) -> None:
    for courier_id in courier_ids:
        couriers.go_online(session, courier_id=courier_id, now=now)


def ready_otc_order(session, *, qty=1, now=NOON):
    """An OTC order a chemist has accepted, so it is waiting for a courier."""
    order = place(session, otc(session, qty), now=now)
    offer = dispatching.open_offer(session, order.id)
    dispatching.accept_offer(session, offer_id=offer.id, chemist_id=offer.chemist_id, now=now)
    assert order.status is OrderStatus.READY_FOR_PICKUP
    return order


def ready_h1_order(session, *, now=NOON):
    """A Schedule H1 order through both gates: the basket that writes a register row."""
    prescription = upload(session)
    azithro = mid(session, "Azithromycin 500mg")
    order = place(session, [LineRequest(azithro, 1)], prescription_id=prescription.id, now=now)
    offer = dispatching.open_offer(session, order.id)
    dispatching.accept_offer(session, offer_id=offer.id, chemist_id=offer.chemist_id, now=now)
    ordering.verify_prescription(
        session,
        prescription_id=prescription.id,
        authorisations=[(azithro, 1)],
        pharmacist="Ph. R Nair",
    )
    assert order.status is OrderStatus.READY_FOR_PICKUP
    return order


def assign(session, order_id: int, *, now=NOON) -> int:
    """Offer the delivery and have the offered courier accept. Returns their id."""
    offer = couriers.offer_delivery(session, order_id=order_id, now=now)
    assert offer is not None
    couriers.accept_delivery(session, offer_id=offer.id, courier_id=offer.courier_id, now=now)
    return offer.courier_id


def stop_for(session, *, courier_id: int, order_id: int, kind: StopKind) -> int:
    plan = couriers.plan_for(session, courier_id)
    return next(s.stop_id for s in plan.stops if s.order_id == order_id and s.kind is kind)


def collect(session, *, courier_id: int, order_id: int, now: datetime = COLLECTED_AT):
    """Complete the collection stop, which IS the dispensing event."""
    return couriers.complete_stop(
        session,
        stop_id=stop_for(session, courier_id=courier_id, order_id=order_id, kind=StopKind.COLLECT),
        courier_id=courier_id,
        now=now,
    )


def hand_over(session, *, courier_id: int, order_id: int, now: datetime = HANDED_OVER_AT):
    return couriers.complete_stop(
        session,
        stop_id=stop_for(
            session, courier_id=courier_id, order_id=order_id, kind=StopKind.HAND_OVER
        ),
        courier_id=courier_id,
        now=now,
    )


def register_rows(session, order_id: int) -> list[RegisterEntry]:
    return list(session.scalars(select(RegisterEntry).where(RegisterEntry.order_id == order_id)))


# --- Shift and position ----------------------------------------------------


def test_going_online_leaves_a_courier_idle_until_there_is_work(seeded):
    courier = couriers.go_online(seeded, courier_id=1, now=NOON)
    assert courier.state is CourierState.IDLE
    assert courier.last_seen_at == NOON


def test_reporting_a_position_updates_the_couriers_latest_fix(seeded):
    online(seeded, 1)
    courier = couriers.report_position(
        seeded, courier_id=1, lat=VAISHALI[0], lng=VAISHALI[1], now=NOON
    )
    assert (courier.last_lat, courier.last_lng) == VAISHALI
    assert courier.last_cell


def test_reporting_a_position_appends_to_the_position_trail(seeded):
    online(seeded, 1)
    couriers.report_position(seeded, courier_id=1, lat=MALVIYA[0], lng=MALVIYA[1], now=NOON)
    couriers.report_position(
        seeded, courier_id=1, lat=VAISHALI[0], lng=VAISHALI[1], now=NOON + timedelta(minutes=2)
    )
    trail = list(
        seeded.scalars(select(CourierPosition).where(CourierPosition.courier_id == 1))
    )
    assert len(trail) == 2


def test_accepting_a_delivery_moves_the_courier_onto_a_plan(seeded):
    """Availability, not location: ON_PLAN means there is at least one open stop."""
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    assert couriers.plan_for(seeded, courier_id).open_stops
    assert seeded.get(Courier, courier_id).state is CourierState.ON_PLAN


def test_a_courier_returns_to_idle_once_every_stop_is_worked(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    hand_over(seeded, courier_id=courier_id, order_id=order.id)
    assert seeded.get(Courier, courier_id).state is CourierState.IDLE


def test_an_inactive_courier_cannot_start_a_shift(seeded):
    seeded.get(Courier, 3).is_active = False
    seeded.flush()
    with pytest.raises(CourierError, match="not active"):
        couriers.go_online(seeded, courier_id=3, now=NOON)


# --- Candidate selection ---------------------------------------------------


def test_a_courier_beyond_the_pickup_cap_is_not_a_candidate(seeded):
    """Uber's wild-goose-chase lesson, applied to the pickup leg.

    Under excess demand a matcher that keeps reaching further for supply turns
    pickup time into deadweight and throughput collapses for everyone, so
    `max_courier_pickup_km` is a hard cap rather than a ranking penalty.
    """
    online(seeded, 1, 2)
    couriers.report_position(
        seeded, courier_id=2, lat=VAISHALI[0], lng=VAISHALI[1], now=NOON
    )
    candidates = couriers.available_couriers(
        seeded, chemist_lat=MALVIYA[0], chemist_lng=MALVIYA[1]
    )
    assert [c.courier_id for c in candidates] == [1]


def test_a_cold_chain_basket_only_reaches_a_cold_chain_courier(seeded):
    """A courier without a cold box cannot carry insulin whatever their ETA."""
    online(seeded, 1, 2, 3, 4)
    candidates = couriers.available_couriers(
        seeded,
        chemist_lat=MALVIYA[0],
        chemist_lng=MALVIYA[1],
        needs_cold_chain=True,
    )
    assert [c.courier_id for c in candidates] == [1]


def test_an_offline_courier_is_not_a_candidate(seeded):
    online(seeded, 1, 2)
    couriers.go_offline(seeded, courier_id=2, now=NOON)
    candidates = couriers.available_couriers(
        seeded, chemist_lat=MALVIYA[0], chemist_lng=MALVIYA[1]
    )
    assert [c.courier_id for c in candidates] == [1]


# --- Delivery offers -------------------------------------------------------


def test_an_order_still_being_sourced_is_not_offered_to_a_courier(seeded):
    """Summoning a courier before a counter is ready just parks them at it."""
    order = place(seeded, otc(seeded))
    assert order.status is OrderStatus.SEEKING_CHEMIST
    online(seeded, 1)
    assert couriers.offer_delivery(seeded, order_id=order.id, now=NOON) is None


def test_accepting_a_delivery_appends_a_collection_then_a_handover(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    offer = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    plan = couriers.accept_delivery(seeded, offer_id=offer.id, courier_id=1, now=NOON)
    assert [s.kind for s in plan.stops] == [StopKind.COLLECT, StopKind.HAND_OVER]
    assert [s.sequence for s in plan.stops] == [1, 2]


def test_the_collection_stop_sits_at_the_chemist_and_the_handover_at_the_patient(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collection, handover = couriers.plan_for(seeded, courier_id).stops
    chemist = seeded.get(Chemist, order.chemist_id)
    assert (collection.lat, collection.lng) == (chemist.lat, chemist.lng)
    assert (handover.lat, handover.lng) == (order.delivery_lat, order.delivery_lng)


def test_a_courier_cannot_accept_an_offer_made_to_someone_else(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    offer = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    with pytest.raises(CourierError, match="not made to you"):
        couriers.accept_delivery(seeded, offer_id=offer.id, courier_id=2, now=NOON)


def test_declining_a_delivery_offers_it_to_the_next_courier(seeded):
    """A courier refusing is a routing event, not the death of the delivery."""
    order = ready_otc_order(seeded)
    online(seeded, 1, 2)
    first = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    couriers.decline_delivery(
        seeded, offer_id=first.id, courier_id=first.courier_id, reason="on a break", now=NOON
    )
    second = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    assert first.state is OfferState.DECLINED
    assert second is not None
    assert second.courier_id != first.courier_id
    assert second.attempt_seq == first.attempt_seq + 1


def test_only_one_delivery_offer_is_open_for_an_order_at_a_time(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1, 2)
    first = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    assert couriers.offer_delivery(seeded, order_id=order.id, now=NOON) is None
    assert couriers.open_courier_offer(seeded, order.id).id == first.id


def test_a_delivery_offer_answered_after_its_window_loses_it(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    offer = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    late = offer.expires_at + timedelta(seconds=1)
    with pytest.raises(CourierError, match="expired"):
        couriers.accept_delivery(seeded, offer_id=offer.id, courier_id=1, now=late)
    assert offer.state is OfferState.EXPIRED


def test_an_order_already_on_a_plan_is_not_offered_again(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1, 2)
    assign(seeded, order.id)
    assert couriers.offer_delivery(seeded, order_id=order.id, now=NOON) is None


# --- Custody: the reason couriers have per-person identity -----------------


def test_a_collection_names_the_courier_who_actually_collected_on_the_register(seeded):
    """The whole reason a courier carries a per-person token.

    The Schedule H1 register has to record WHO took the package out of the
    pharmacy. The first courier offered the job is not necessarily the one who
    did it, so the custody line must come from the courier who completed the
    collection, never from the offer that happened to be made first.
    """
    order = ready_h1_order(seeded)
    online(seeded, 1, 2)
    offered_first = couriers.offer_delivery(seeded, order_id=order.id, now=NOON)
    couriers.decline_delivery(
        seeded,
        offer_id=offered_first.id,
        courier_id=offered_first.courier_id,
        reason="already carrying two",
        now=NOON,
    )
    collector = assign(seeded, order.id)
    assert collector != offered_first.courier_id

    collect(seeded, courier_id=collector, order_id=order.id)
    rows = register_rows(seeded, order.id)
    assert len(rows) == 1
    assert rows[0].custody_courier_id == collector


def test_a_collection_is_the_dispensing_event_for_the_whole_order(seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    assert order.status is OrderStatus.OUT_FOR_DELIVERY
    assert order.dispensed_at == COLLECTED_AT


def test_a_courier_holding_collected_goods_cannot_end_their_shift(seeded):
    """Custody is a compliance fact: the goods belong to a chemist's register."""
    order = ready_h1_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    with pytest.raises(CustodyHeld, match="holding dispensed goods"):
        couriers.go_offline(seeded, courier_id=courier_id, now=COLLECTED_AT)
    assert seeded.get(Courier, courier_id).state is CourierState.ON_PLAN


def test_a_courier_can_end_their_shift_once_the_goods_are_handed_over(seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    hand_over(seeded, courier_id=courier_id, order_id=order.id)
    courier = couriers.go_offline(seeded, courier_id=courier_id, now=HANDED_OVER_AT)
    assert courier.state is CourierState.OFFLINE


def test_completing_the_same_collection_twice_writes_one_register_row(seeded):
    """At-least-once delivery makes a replayed completion a question of when, not if.

    A duplicated row here would falsify a statutory register and double-debit
    the patient's authorised quantity, so the second call must change nothing.
    """
    order = ready_h1_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    stop_id = stop_for(seeded, courier_id=courier_id, order_id=order.id, kind=StopKind.COLLECT)
    couriers.complete_stop(seeded, stop_id=stop_id, courier_id=courier_id, now=COLLECTED_AT)
    with pytest.raises(IllegalStopTransition):
        couriers.complete_stop(
            seeded, stop_id=stop_id, courier_id=courier_id, now=COLLECTED_AT + timedelta(minutes=1)
        )
    assert len(register_rows(seeded, order.id)) == 1


def test_an_otc_collection_writes_no_register_row_but_still_takes_custody(seeded):
    """Custody is not readable from the register: OTC lines never write one."""
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    assert register_rows(seeded, order.id) == []
    assert couriers.plan_for(seeded, courier_id).holds_custody_of(order.id)


def test_a_stop_on_someone_elses_plan_cannot_be_completed(seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1, 2)
    courier_id = assign(seeded, order.id)
    other = 2 if courier_id != 2 else 1
    stop_id = stop_for(seeded, courier_id=courier_id, order_id=order.id, kind=StopKind.COLLECT)
    with pytest.raises(CourierError, match="not on your plan"):
        couriers.complete_stop(seeded, stop_id=stop_id, courier_id=other, now=COLLECTED_AT)
    assert register_rows(seeded, order.id) == []


# --- Handover --------------------------------------------------------------


def test_completing_the_handover_delivers_the_order_inside_the_promise(seeded):
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    hand_over(seeded, courier_id=courier_id, order_id=order.id)
    assert order.status is OrderStatus.DELIVERED
    assert order.delivered_at == HANDED_OVER_AT
    assert order.promise_honoured is True


def test_a_handover_after_the_promise_expired_is_recorded_as_a_breach(seeded):
    """The promise is measured, not assumed: a late delivery says so on the order."""
    order = ready_otc_order(seeded)
    online(seeded, 1)
    courier_id = assign(seeded, order.id)
    collect(seeded, courier_id=courier_id, order_id=order.id)
    hand_over(seeded, courier_id=courier_id, order_id=order.id, now=NOON + timedelta(minutes=75))
    assert order.status is OrderStatus.DELIVERED
    assert order.promise_honoured is False


# --- HTTP surface ----------------------------------------------------------


def courier_token(courier_id: int) -> dict[str, str]:
    """Couriers authenticate per person, not with a shared role token."""
    return {"X-Medic-Token": f"dev-courier-{courier_id}"}


def test_a_token_belonging_to_no_courier_is_refused(client):
    response = client.post("/api/courier/1/online", headers={"X-Medic-Token": "dev-courier-9"})
    assert response.status_code == 403
    assert "courier token" in response.json()["detail"]


def test_one_couriers_token_cannot_act_on_another_couriers_account(client):
    response = client.post("/api/courier/2/online", headers=courier_token(1))
    assert response.status_code == 403
    assert "not your courier account" in response.json()["detail"]


def test_going_offline_while_holding_goods_is_a_409_custody_conflict(client, seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1)
    assign(seeded, order.id)
    collect(seeded, courier_id=1, order_id=order.id)
    response = client.post("/api/courier/1/offline", headers=courier_token(1))
    assert response.status_code == 409
    assert response.json()["error"] == "custody_held"


def test_the_plan_endpoint_labels_each_stop_readably(client, seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1)
    assign(seeded, order.id)
    chemist = seeded.get(Chemist, order.chemist_id)
    labels = [
        row["label"]
        for row in client.get("/api/courier/1/plan", headers=courier_token(1)).json()
    ]
    assert labels == [
        f"Collect order #{order.id} from {chemist.name}",
        f"Hand over order #{order.id} in {order.delivery_zone}",
    ]


def test_the_courier_console_reports_what_the_courier_is_holding(client, seeded):
    order = ready_h1_order(seeded)
    online(seeded, 1)
    assign(seeded, order.id)
    collect(seeded, courier_id=1, order_id=order.id)
    body = client.get("/api/courier/1/me", headers=courier_token(1)).json()
    assert body["orders_in_custody"] == [order.id]
