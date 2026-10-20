"""When the counter will actually be ready, and when to summon a courier for it.

The pickup here is not ready when the order arrives. Uber's dispatcher can send
the nearest driver the instant a rider taps, because the rider is already
standing at the kerb; a chemist counter is not. The package becomes ready at an
uncertain future time gated by two things at once: the chemist physically
picking and packing the basket, and a human pharmacist reaching it in a
verification queue. Dispatching the nearest courier at order time therefore buys
nothing -- it converts courier idle time into courier-waiting-at-a-counter time,
both of which are spent inside the same 60 minutes, and the second kind also
holds a courier out of the pool for the next order.

So the sequence is inverted: predict `ready_at` first, then schedule the courier
BACKWARDS from it so they arrive just before the package does, with
`config.courier_early_arrival_minutes` of deliberate slack in the direction that
makes the courier wait a little rather than the package wait a lot.

The concurrency assumption is `geo.EtaBreakdown`'s and is not restated
independently: prep and verification overlap, so the slower of the two gates
sets the pickup time, never their sum. `verification_minutes(1)` -- a basket
that needs a pharmacist with nobody queued ahead of it -- returns exactly the
flat `rx_verification_target_minutes` that `geo.estimate_eta` uses, so the two
modules cannot disagree about an unqueued order.

HONESTY ABOUT WHAT THIS IS. Every number below is a deterministic estimator
built out of rolling means and configured constants. Nothing here is fitted, and
no prediction it makes has yet been scored against reality, so `confidence`
reports how much HISTORY backed an estimate and not how accurate it was. The
right next step is not a better functional form, and certainly not a model: it
is to MEASURE the error. `Order.eta_minutes_at_checkout` and `Order.delivered_at`
are already stored, so the join that yields predicted-versus-realized per order
is nearly free, and the residuals it produces are what would tell us whether
prep variance, queue depth, or the road factor dominates. Fit only after that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from medic.config import Settings, settings

#: Baskets needed in the last-hour window before it is allowed to outweigh the
#: multi-day mean. Below this, one awkward order would swing the estimate for
#: every order queued behind it.
MIN_RECENT_SAMPLES = 3

#: Weight on the short window once it clears `MIN_RECENT_SAMPLES`. Uber Eats'
#: prep model pairs a multi-day aggregate with a last-hour one and leans on the
#: recent half for the reason that applies here too: what has changed since
#: yesterday is the queue standing in the shop right now.
RECENT_WEIGHT = 0.7

#: Sample counts at which a prep estimate stops being mostly guesswork.
_MEDIUM_CONFIDENCE_SAMPLES = 10
_HIGH_CONFIDENCE_SAMPLES = 40


@dataclass(frozen=True)
class PrepEstimate:
    """One chemist's rolling prep statistics, as the predictor sees them.

    Both means are optional because an empty window is what a SQL `AVG` over no
    rows returns, and a chemist with no history at all is the common case on
    their first day.
    """

    sample_count: int = 0
    mean_minutes: float | None = None
    recent_sample_count: int = 0
    recent_mean_minutes: float | None = None
    #: Carried as data rather than read from config inside `.minutes`, so the
    #: blend stays a property while the tunable stays injectable per call
    #: through `blend_prep`.
    fallback_minutes: int = settings.default_chemist_prep_minutes

    @property
    def minutes(self) -> int:
        """Minutes of prep to plan for."""
        long_mean = self.mean_minutes if self.sample_count > 0 else None
        recent_mean = self.recent_mean_minutes if self.recent_sample_count > 0 else None
        if long_mean is None and recent_mean is None:
            return self.fallback_minutes
        if recent_mean is None:
            return math.ceil(long_mean)
        if long_mean is None:
            # Even one or two of this chemist's own baskets beat a platform-wide
            # default, which is about no chemist in particular.
            return math.ceil(recent_mean)
        weight = RECENT_WEIGHT if self.recent_sample_count >= MIN_RECENT_SAMPLES else 0.0
        # Rounding up only keeps this comparable with the integer minute budgets
        # the rest of the domain works in; the bias that protects the package
        # lives in `courier_early_arrival_minutes`, in one place.
        return math.ceil(recent_mean * weight + long_mean * (1.0 - weight))


def blend_prep(
    long_mean: float | None,
    long_n: int,
    recent_mean: float | None,
    recent_n: int,
    *,
    config: Settings = settings,
) -> PrepEstimate:
    """Bundle a chemist's two prep aggregates into an estimate.

    Thin on purpose: this is the one place the configured fallback is read, so
    `PrepEstimate.minutes` needs no config argument and stays a property.
    """
    return PrepEstimate(
        sample_count=long_n,
        mean_minutes=long_mean,
        recent_sample_count=recent_n,
        recent_mean_minutes=recent_mean,
        fallback_minutes=config.default_chemist_prep_minutes,
    )


def verification_minutes(
    queue_depth: int,
    *,
    pharmacists_on_duty: int = 1,
    config: Settings = settings,
) -> int:
    """Minutes until a pharmacist has cleared THIS order.

    `queue_depth` counts this order's own verification, so a depth of 1 is an
    empty queue and returns exactly the flat `rx_verification_target_minutes`
    that `geo.estimate_eta` quotes. A depth of 0 means the basket needs no
    pharmacist at all -- the caller passes a depth only when it does.

    The queue is the part of the 60 minutes that no amount of courier
    positioning can recover, which is why its depth is modelled explicitly
    rather than folded into the prep mean.
    """
    if queue_depth <= 0:
        return 0
    if pharmacists_on_duty < 1:
        # Clamping to one would quietly claim somebody is working a queue that
        # nobody is working, and the resulting ready_at would be fiction.
        raise ValueError("verification needs at least one pharmacist on duty")
    ahead = queue_depth - 1
    queued = math.ceil(ahead / pharmacists_on_duty) * config.verification_minutes_per_queued_order
    return config.rx_verification_target_minutes + queued


def predict_ready_at(
    now: datetime,
    *,
    prep: PrepEstimate,
    verification_minutes: int,
) -> datetime:
    """When the package will be on the counter, ready for collection.

    Travel is deliberately absent: this is the moment the goods are ready, not
    the moment they could reach the customer. Prep and verification run
    concurrently, so the slower gate sets the time -- the same assumption
    `geo.EtaBreakdown.total_minutes` makes.
    """
    return now + timedelta(minutes=max(prep.minutes, verification_minutes))


def courier_summon_at(
    ready_at: datetime,
    travel_minutes_to_chemist: int,
    *,
    config: Settings = settings,
) -> datetime:
    """When to start a courier moving so they arrive just before `ready_at`.

    A summon time in the past is not an error: it is the signal that the courier
    is already late and should be summoned now. Comparing it against a clock is
    the caller's job, because this layer holds no clock of its own.
    """
    lead = travel_minutes_to_chemist + config.courier_early_arrival_minutes
    return ready_at - timedelta(minutes=lead)


@dataclass(frozen=True)
class ReadinessPrediction:
    """One prediction of counter readiness, carrying the parts it was built from.

    `predicted_at` is a field rather than a clock read so a prediction can be
    replayed, and so the residual against a realized `delivered_at` can be
    computed later without having to guess when the prediction was made.
    """

    predicted_at: datetime
    ready_at: datetime
    prep: PrepEstimate
    verification_minutes: int

    @property
    def confidence(self) -> str:
        """How much history backed the prep estimate: none | low | medium | high.

        An ordinal label for a sample count, NOT a calibrated accuracy -- no
        prediction from this module has been scored against a realized arrival
        yet. It is useful for deciding how much slack to quote on top of a
        prediction, and misleading if read as a probability.
        """
        # The last-hour window is a subset of the multi-day one, so these are
        # compared rather than added.
        samples = max(self.prep.sample_count, self.prep.recent_sample_count)
        if samples <= 0:
            return "none"
        if samples < _MEDIUM_CONFIDENCE_SAMPLES:
            return "low"
        if samples < _HIGH_CONFIDENCE_SAMPLES:
            return "medium"
        return "high"
