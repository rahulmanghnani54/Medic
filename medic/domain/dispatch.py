"""The offer loop.

This is the piece of Uber's architecture that transfers most directly. An
order is not assigned to a chemist at checkout; it is OFFERED to a ranked
candidate, who may accept, decline, or let the offer expire. Any non-acceptance
returns the order to sourcing and the next candidate is tried. Only exhausting
the candidates or the delivery budget closes the order.

Two things make this different from ride-hailing dispatch, and both are
load-bearing:

1. An accept here is a CONDITIONAL commitment. A chemist accepts on the
   strength of their declared stock; shelf truth is a separate fact revealed
   later. So WITHDRAWN is a distinct outcome from DECLINED, it happens after
   acceptance, and it must re-enter sourcing rather than kill the order.

2. Chemist stock is depletable, so an offer must hold a lease on the units it
   is about. Uber can broadcast one trip to many drivers because drivers are
   not consumed by being offered. Offering one order to several chemists
   without a lease would let two of them accept against the same units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class OfferState(StrEnum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    # The offeree never answered inside the window.
    EXPIRED = "EXPIRED"
    # Accepted, then handed back: the shelf did not match the declared stock.
    WITHDRAWN = "WITHDRAWN"
    # Still open when the order stopped needing it (customer cancelled, the
    # prescription was rejected, or the budget ran out).
    SUPERSEDED = "SUPERSEDED"


#: Outcomes that return the order to sourcing rather than ending it. This is
#: Uber's asymmetry: the supply side declining is a routing event, not a
#: failure of the request.
RESOURCEABLE: frozenset[OfferState] = frozenset(
    {OfferState.DECLINED, OfferState.EXPIRED, OfferState.WITHDRAWN}
)

TERMINAL_OFFER: frozenset[OfferState] = frozenset(
    {
        OfferState.DECLINED,
        OfferState.EXPIRED,
        OfferState.WITHDRAWN,
        OfferState.SUPERSEDED,
    }
)

ALLOWED_OFFER: dict[OfferState, frozenset[OfferState]] = {
    OfferState.OFFERED: frozenset(
        {
            OfferState.ACCEPTED,
            OfferState.DECLINED,
            OfferState.EXPIRED,
            OfferState.SUPERSEDED,
        }
    ),
    # An accepted offer can still be withdrawn, right up until the courier
    # takes custody. After that the goods have left the shelf and the order is
    # no longer the chemist's to hand back.
    OfferState.ACCEPTED: frozenset({OfferState.WITHDRAWN}),
    OfferState.DECLINED: frozenset(),
    OfferState.EXPIRED: frozenset(),
    OfferState.WITHDRAWN: frozenset(),
    OfferState.SUPERSEDED: frozenset(),
}


class IllegalOfferTransition(Exception):
    def __init__(self, current: OfferState, requested: OfferState) -> None:
        super().__init__(f"cannot move an offer from {current} to {requested}")
        self.current = current
        self.requested = requested


def assert_offer_transition(current: OfferState, requested: OfferState) -> None:
    if requested not in ALLOWED_OFFER.get(current, frozenset()):
        raise IllegalOfferTransition(current, requested)


def is_expired(offered_at: datetime, window_minutes: int, now: datetime) -> bool:
    """Whether an open offer has run past its window.

    The expiry time is written onto the offer row when it is created, in the
    same transaction as the state change, so a crashed dispatcher cannot lose
    the timer. This function is the pure predicate the sweep uses.
    """
    return now >= offered_at + timedelta(minutes=window_minutes)


@dataclass(frozen=True)
class OfferDecision:
    """What the dispatcher decided to do for one order on one pass."""

    action: str  # 'offer' | 'wait' | 'exhausted'
    chemist_id: int | None = None
    reason: str = ""
    attempt_seq: int = 0


def next_offer(
    *,
    ranked_chemist_ids: list[int],
    already_tried: set[int],
    open_offer_exists: bool,
    attempt_seq: int,
    max_attempts: int,
    budget_allows: bool,
) -> OfferDecision:
    """Decide the next dispatch action for one order. Pure.

    Sequential rather than broadcast: exactly one chemist holds an open offer
    at a time, because an offer leases stock and a broadcast lease would hold
    the same units at several pharmacies at once.
    """
    if open_offer_exists:
        return OfferDecision(
            action="wait", reason="an offer is already open", attempt_seq=attempt_seq
        )
    if not budget_allows:
        return OfferDecision(
            action="exhausted",
            reason="the delivery budget cannot accommodate another offer round",
            attempt_seq=attempt_seq,
        )
    if attempt_seq >= max_attempts:
        return OfferDecision(
            action="exhausted",
            reason=f"reached the limit of {max_attempts} sourcing attempts",
            attempt_seq=attempt_seq,
        )
    for chemist_id in ranked_chemist_ids:
        if chemist_id not in already_tried:
            return OfferDecision(
                action="offer", chemist_id=chemist_id, attempt_seq=attempt_seq + 1
            )
    return OfferDecision(
        action="exhausted",
        reason="every candidate chemist has already been tried",
        attempt_seq=attempt_seq,
    )
