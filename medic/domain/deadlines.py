"""The delivery promise as a budget that is spent down, not a date that is checked.

Two defects motivated this module, and both come from treating the 60 minutes as
a number written once rather than as a resource being consumed.

Measurement first. `promised_minutes` is stamped at checkout and `sla_view`
measures from `created_at`, so the minutes spent searching for a chemist and
re-offering a declined order come out of the customer's hour with nothing in the
model noticing. A budget makes that spend explicit, and `BudgetLedger` answers
the question a bare deadline cannot: where did the hour actually go?

Feasibility second. `geo.promise_is_feasible` asks whether a candidate fits the
FULL promise, and it is asked once, at checkout. A chemist predicted to need 48
minutes clears a 60-minute promise then, and is still judged to clear it after
two offer rounds have timed out -- by which point the remaining budget cannot
hold them. `DeliveryBudget.admits` is that same question asked against what is
actually left, so the dispatcher can re-check it before every offer instead of
trusting a verdict reached before the clock started running.

The at-risk threshold is deliberately not restated here: `at_risk` reads it from
`orders.SlaView`, which stays its single definition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from medic.config import Settings, settings
from medic.domain.orders import sla_view

#: Quoted bands are rounded to this grain. A band that shifts minute by minute
#: claims a precision we do not have, and tempts the customer into reading its
#: low end as the promise.
BAND_GRAIN_MINUTES = 5

#: Stage names in the order they cut the timeline.
STAGES: tuple[str, ...] = ("sourcing", "verification", "prep", "transport")


def _floor_minutes(delta: timedelta) -> int:
    """Whole minutes in `delta`, rounding towards minus infinity.

    Matches `orders.sla_view`, so a budget and the SLA view can never disagree
    about whether an order has a minute left.
    """
    return int(delta.total_seconds() // 60)


def _span_minutes(start: datetime, end: datetime) -> int:
    return max(0, _floor_minutes(end - start))


@dataclass(frozen=True)
class DeliveryBudget:
    """What is left of one order's promise, measured from when it was taken."""

    created_at: datetime
    promised_minutes: int

    @property
    def deadline(self) -> datetime:
        return self.created_at + timedelta(minutes=self.promised_minutes)

    def elapsed_minutes(self, now: datetime) -> int:
        return _floor_minutes(now - self.created_at)

    def remaining_minutes(self, now: datetime) -> int:
        """Minutes of promise left. Negative once the promise is broken."""
        return _floor_minutes(self.deadline - now)

    def spent_fraction(self, now: datetime) -> float:
        """Share of the promise consumed so far."""
        if self.promised_minutes <= 0:
            return 1.0  # a nonsense promise is treated as already gone
        # Deliberately uncapped: the console needs to see how far past the
        # promise an order has run, not merely that it has.
        return max(0.0, self.elapsed_minutes(now) / self.promised_minutes)

    def admits(
        self,
        predicted_work_minutes: int,
        now: datetime,
        *,
        margin_minutes: int | None = None,
        config: Settings = settings,
    ) -> bool:
        """Whether what is LEFT of the budget still holds this much work plus margin.

        The per-attempt form of `geo.promise_is_feasible`, which weighs the same
        margin against the whole promise. Offering to a candidate the remaining
        budget cannot hold spends a further accept window on a chemist who was
        never going to make the window.
        """
        margin = (
            config.sla_feasibility_margin_minutes if margin_minutes is None else margin_minutes
        )
        return predicted_work_minutes + margin <= self.remaining_minutes(now)

    def is_breached(self, now: datetime) -> bool:
        return self.remaining_minutes(now) < 0

    def at_risk(self, now: datetime) -> bool:
        """Near enough to the deadline to escalate, but not yet breached.

        Delegates to `orders.sla_view` rather than restating its threshold: a
        budget calling an order at risk while the customer-facing SLA view did
        not would have the console and the customer reading one order two
        different ways.
        """
        return sla_view(self.created_at, self.promised_minutes, now).at_risk


def _consecutive_spans(
    start: datetime, marks: tuple[datetime | None, ...], end: datetime
) -> tuple[int, ...]:
    """Minutes between consecutive milestones: one span per mark.

    A stage whose closing milestone has simply not been reached yet runs to
    `end`. One whose milestone is missing while a LATER milestone is known is a
    recording gap and gets nothing, so the gap surfaces as unattributed time
    rather than inflating whichever stage happens to sit next to it.
    """
    spans: list[int] = []
    opens_at: datetime | None = start
    for position, mark in enumerate(marks):
        if opens_at is None:
            spans.append(0)
            # A known milestone after a gap re-opens the chain: the stages after
            # it stay measurable even though the gap itself is not.
            opens_at = mark
            continue
        if mark is not None:
            # Milestones are written by separate actors, so clamp rather than
            # trust that they arrived in order.
            closes_at = min(max(mark, opens_at), end)
            spans.append(_span_minutes(opens_at, closes_at))
            opens_at = closes_at
            continue
        later_known = any(m is not None for m in marks[position + 1:])
        spans.append(0 if later_known else _span_minutes(opens_at, end))
        opens_at = None
    return tuple(spans)


@dataclass(frozen=True)
class BudgetLedger:
    """Where one order's hour went, attributed to stages.

    Prep and verification genuinely overlap, so charging each its own wall-clock
    span would make a 60-minute order report 75 minutes of work and point the
    console at the wrong gate. The convention here is a strict partition of the
    timeline instead: the order is charged to verification until the pharmacist
    clears it and to prep thereafter, because once verification has cleared,
    prep is provably the only gate still holding the order.

    `as_of` is a field rather than a clock read, so a ledger is reproducible for
    any moment -- including replaying a breach that happened last week.
    """

    budget: DeliveryBudget
    as_of: datetime
    accepted_at: datetime | None = None
    verified_at: datetime | None = None
    ready_at: datetime | None = None
    delivered_at: datetime | None = None

    @property
    def horizon(self) -> datetime:
        """The moment attribution stops."""
        if self.delivered_at is None:
            return self.as_of
        # A delivered order's hour ended at handover. Measuring it to `now`
        # would grow every finished order's unattributed time for ever.
        return min(self.delivered_at, self.as_of)

    def breakdown(self) -> dict[str, int]:
        """Minutes attributed to each stage. Always carries all four keys."""
        end = self.horizon
        sourcing, fulfilment, transport = _consecutive_spans(
            self.budget.created_at, (self.accepted_at, self.ready_at, self.delivered_at), end
        )
        # The verification cut is drawn inside the fulfilment window only when
        # we hold its timestamp: a missing `verified_at` means either that the
        # basket needed no pharmacist or that one has not finished yet, and
        # neither is time we can honestly charge to verification.
        verification = 0
        if self.accepted_at is not None and self.verified_at is not None:
            verification = min(fulfilment, _span_minutes(self.accepted_at, self.verified_at))
        return {
            "sourcing": sourcing,
            "verification": verification,
            "prep": fulfilment - verification,
            "transport": transport,
        }

    @property
    def unattributed_minutes(self) -> int:
        """Elapsed minutes no stage accounts for.

        Anything beyond rounding means a milestone is missing from the middle of
        the timeline rather than merely unreached, which is a recording gap
        worth showing instead of silently loading onto a neighbouring stage.
        """
        accounted = sum(self.breakdown().values())
        return max(0, self.budget.elapsed_minutes(self.horizon) - accounted)


def promise_band(eta_minutes: int, *, config: Settings = settings) -> tuple[int, int]:
    """A low/high window to quote while no chemist is bound yet.

    A quote is built from the best-ranked candidate's ETA, which assumes that
    candidate accepts the first offer. That is the optimistic case: one
    timed-out offer round costs the whole accept window before sourcing even
    moves on. So the band carries the cost of one such round on its high end,
    and nothing firmer than a band is stated until a chemist has accepted -- a
    quote naming a single number would be read as the promise.
    """
    low = max(0, eta_minutes)
    high = low + config.chemist_accept_window_minutes
    return (
        low // BAND_GRAIN_MINUTES * BAND_GRAIN_MINUTES,
        math.ceil(high / BAND_GRAIN_MINUTES) * BAND_GRAIN_MINUTES,
    )
