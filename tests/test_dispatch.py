"""The offer loop: the offer state machine, the expiry window and next_offer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medic.domain.dispatch import (
    ALLOWED_OFFER,
    RESOURCEABLE,
    TERMINAL_OFFER,
    IllegalOfferTransition,
    OfferState,
    assert_offer_transition,
    is_expired,
    next_offer,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
WINDOW_MINUTES = 5

OUTCOMES = (
    OfferState.ACCEPTED,
    OfferState.DECLINED,
    OfferState.EXPIRED,
    OfferState.SUPERSEDED,
)


def decide(**overrides):
    """next_offer on a healthy order: no open offer, budget intact, nothing tried."""
    kwargs = {
        "ranked_chemist_ids": [11, 22, 33],
        "already_tried": set(),
        "open_offer_exists": False,
        "attempt_seq": 0,
        "max_attempts": 3,
        "budget_allows": True,
    }
    return next_offer(**{**kwargs, **overrides})


def test_an_open_offer_can_reach_all_four_outcomes():
    for outcome in OUTCOMES:
        assert_offer_transition(OfferState.OFFERED, outcome)


def test_an_open_offer_cannot_be_withdrawn_before_anyone_has_accepted_it():
    """Withdrawal is a chemist reneging on their own acceptance, so it needs one."""
    with pytest.raises(IllegalOfferTransition):
        assert_offer_transition(OfferState.OFFERED, OfferState.WITHDRAWN)


def test_an_accepted_offer_can_be_withdrawn():
    assert_offer_transition(OfferState.ACCEPTED, OfferState.WITHDRAWN)


def test_withdrawal_is_the_only_move_left_after_an_acceptance():
    for requested in OfferState:
        if requested is OfferState.WITHDRAWN:
            continue
        with pytest.raises(IllegalOfferTransition):
            assert_offer_transition(OfferState.ACCEPTED, requested)


def test_every_terminal_offer_state_is_absorbing():
    for terminal in TERMINAL_OFFER:
        for requested in OfferState:
            with pytest.raises(IllegalOfferTransition):
                assert_offer_transition(terminal, requested)


def test_the_transition_table_covers_every_offer_state():
    assert set(ALLOWED_OFFER) == set(OfferState)


def test_an_offer_cannot_be_re_issued_while_it_is_still_open():
    with pytest.raises(IllegalOfferTransition):
        assert_offer_transition(OfferState.OFFERED, OfferState.OFFERED)


def test_an_illegal_transition_reports_both_ends_of_the_move():
    with pytest.raises(IllegalOfferTransition) as raised:
        assert_offer_transition(OfferState.DECLINED, OfferState.ACCEPTED)
    assert raised.value.current is OfferState.DECLINED
    assert raised.value.requested is OfferState.ACCEPTED
    assert "DECLINED" in str(raised.value)
    assert "ACCEPTED" in str(raised.value)


def test_a_decline_returns_the_order_to_sourcing_rather_than_killing_it():
    """RESOURCEABLE is exactly the three outcomes that hand the order back to sourcing.

    The asymmetry matters because it decides who absorbs a chemist's "no". A
    decline, a silent expiry and a post-acceptance withdrawal are all supply-side
    events: the patient still wants the medicine, so the order re-enters sourcing
    and the next ranked chemist is tried. If DECLINED were treated as terminal,
    one uninterested pharmacy would cancel an order that four others could have
    filled, and the failure would be billed to the patient.

    SUPERSEDED is the one terminal outcome in the set's shadow, and deliberately
    so: there the *order* stopped needing the offer (cancelled, prescription
    rejected, budget spent), so re-sourcing would be chasing work nobody wants.
    """
    assert RESOURCEABLE == {OfferState.DECLINED, OfferState.EXPIRED, OfferState.WITHDRAWN}
    assert OfferState.SUPERSEDED not in RESOURCEABLE


def test_re_sourceable_outcomes_are_still_terminal_for_the_offer():
    """The order lives on; the offer that was declined does not come back to life."""
    assert RESOURCEABLE <= TERMINAL_OFFER
    for state in RESOURCEABLE:
        assert ALLOWED_OFFER[state] == frozenset()


def test_an_acceptance_is_not_terminal():
    assert OfferState.ACCEPTED not in TERMINAL_OFFER


def test_an_offer_is_live_one_second_before_its_window_closes():
    offered_at = NOW - timedelta(minutes=WINDOW_MINUTES) + timedelta(seconds=1)
    assert not is_expired(offered_at, WINDOW_MINUTES, NOW)


def test_an_offer_is_expired_exactly_on_its_deadline():
    """The boundary is inclusive: a sweep landing on the deadline must reap the offer."""
    offered_at = NOW - timedelta(minutes=WINDOW_MINUTES)
    assert is_expired(offered_at, WINDOW_MINUTES, NOW)


def test_an_offer_is_expired_one_second_after_its_deadline():
    offered_at = NOW - timedelta(minutes=WINDOW_MINUTES) - timedelta(seconds=1)
    assert is_expired(offered_at, WINDOW_MINUTES, NOW)


def test_a_fresh_offer_is_not_expired():
    assert not is_expired(NOW, WINDOW_MINUTES, NOW)


def test_an_order_with_an_offer_already_open_waits():
    decision = decide(open_offer_exists=True, attempt_seq=1)
    assert decision.action == "wait"
    assert decision.chemist_id is None
    assert decision.attempt_seq == 1


def test_waiting_takes_precedence_over_every_reason_to_stop():
    """A lease is outstanding, so nothing may be decided until that offer resolves."""
    decision = decide(
        open_offer_exists=True,
        budget_allows=False,
        attempt_seq=9,
        max_attempts=3,
        already_tried={11, 22, 33},
    )
    assert decision.action == "wait"


def test_an_exhausted_budget_stops_sourcing():
    decision = decide(budget_allows=False)
    assert decision.action == "exhausted"
    assert decision.chemist_id is None
    assert "budget" in decision.reason


def test_hitting_the_attempt_limit_stops_sourcing():
    decision = decide(attempt_seq=3, max_attempts=3)
    assert decision.action == "exhausted"
    assert "3 sourcing attempts" in decision.reason


def test_the_last_attempt_inside_the_limit_still_gets_offered():
    decision = decide(attempt_seq=2, max_attempts=3)
    assert decision.action == "offer"
    assert decision.attempt_seq == 3


def test_running_out_of_untried_chemists_stops_sourcing():
    decision = decide(already_tried={11, 22, 33})
    assert decision.action == "exhausted"
    assert "candidate" in decision.reason


def test_an_empty_candidate_list_is_exhaustion_not_an_error():
    decision = decide(ranked_chemist_ids=[])
    assert decision.action == "exhausted"
    assert "candidate" in decision.reason


def test_the_three_ways_of_running_out_are_told_apart_by_reason():
    """The order's closing status differs by cause, so the reasons cannot collide."""
    reasons = {
        decide(budget_allows=False).reason,
        decide(attempt_seq=3, max_attempts=3).reason,
        decide(already_tried={11, 22, 33}).reason,
    }
    assert len(reasons) == 3


def test_exhaustion_leaves_the_attempt_sequence_where_it_was():
    assert decide(attempt_seq=2, budget_allows=False).attempt_seq == 2
    assert decide(attempt_seq=3, max_attempts=3).attempt_seq == 3
    assert decide(attempt_seq=2, already_tried={11, 22, 33}).attempt_seq == 2


def test_the_highest_ranked_chemist_is_offered_first():
    decision = decide()
    assert decision.action == "offer"
    assert decision.chemist_id == 11


def test_chemists_already_tried_are_skipped_in_rank_order():
    decision = decide(already_tried={11, 22})
    assert decision.chemist_id == 33


def test_making_an_offer_advances_the_attempt_sequence_by_one():
    decision = decide(attempt_seq=1)
    assert decision.action == "offer"
    assert decision.attempt_seq == 2


def test_an_offer_carries_no_reason_text():
    """'offer' is the unremarkable path; reasons exist to explain stopping."""
    assert decide().reason == ""


def test_a_chemist_tried_but_not_in_the_ranking_does_not_block_sourcing():
    decision = decide(ranked_chemist_ids=[11, 22], already_tried={99})
    assert decision.action == "offer"
    assert decision.chemist_id == 11
