# Uber's architecture, mapped onto medicine delivery

This records what was taken from Uber's marketplace design, what was adapted,
and what was deliberately refused. It is the reasoning behind the rebuild in
`medic/domain/dispatch.py`, `medic/services/dispatching.py` and
`medic/services/couriers.py`.

The venture brief already pointed here: *"The app is 'search medicine, we match
the nearest chemist that has it', closer to Uber than Zomato."* That instinct
was right about the **loop** and wrong about the **objective**, and separating
those two is the whole of this document.

## The one-sentence version

**Uber's dispatch loop transfers almost exactly. Uber's matching objective does
not transfer at all.**

The loop — offer, accept or decline or expire, re-offer, with per-attempt state
on an Offer row, a timer committed alongside the state change, and feasibility
re-checked against a deadline on every attempt — is precisely what this system
was missing. The objective — minimise ETA over a fungible supply field, and
clear excess demand with price — breaks on three independent rocks:

1. **Chemists are not fungible.** A pharmacy 300 m away that does not stock the
   molecule is not a worse match. It is not a match. Feasibility is a per-SKU
   inventory-and-legality predicate, not a distance ranking, and two orders in
   the same hex can have disjoint candidate sets.
2. **There is a third party in the middle who can lawfully refuse.** The
   pharmacist's verification can say no, and saying no is the correct outcome.
   Uber has no step where the platform is obliged to decline work it could
   physically perform.
3. **Price is statutorily capped.** MRP and the DPCO/NPPA ceilings are hard
   maxima, and this platform already prices at the cap. There is no headroom,
   so demand cannot be cleared with money.

## What was taken, as-is

| Uber mechanism | Here |
|---|---|
| Offer as a first-class entity, distinct from the request and the supply unit | `Offer` row per (order, chemist, attempt) with its own state machine |
| Offered → accepted / declined / expired, with non-acceptance returning the request to matching | `OfferState`, and `RESOURCEABLE = {DECLINED, EXPIRED, WITHDRAWN}` |
| Asymmetric cancellation: supply declining redispatches, demand cancelling is terminal | `decline_offer` keeps the order alive; `cancel_by_customer` is terminal |
| Accept as one atomic multi-entity write, with supply disappearing as the named race | `accept_offer` wins the offer, binds the chemist and leases stock in one transaction |
| A single strongly-consistent store instead of application-layer sagas | One SQLite transaction via `db.session_scope`; no distributed coordination |
| The expiry timer committed in the same transaction as the state change | `Offer.expires_at`, swept by `expire_due_offers` |
| A statechart per entity, with one enforcement chokepoint each | `assert_can_transition`, `assert_gate_transition`, `assert_offer_transition`, `assert_courier_transition`, `assert_stop_transition` |
| Supply holds an ordered plan of waypoints, not a `current_trip_id` | `WaypointPlan` over `Stop` rows, spanning more than one order |
| Credentials that hard-gate going online when they expire | `Credential`, plus the existing licence-expiry filter |
| ETA, not straight-line distance, as the matching metric | `matching.admits` ranks on `EtaBreakdown.total_minutes` |
| H3 hexagonal indexing for candidate retrieval | `domain/geocells.py` — **Uber's actual H3 library**, not a reimplementation |

## What was adapted

**Batched matching → a courier-leg concern only.** Uber batches because
accumulating candidates beats greedy nearest-driver. That holds for couriers.
It does not hold for chemist selection, where the constraint is stock, not
geometry.

**Surge's supply half → a courier incentive, never a medicine price.** The one
genuinely elastic supply side here is the courier, and the only money that may
move is the delivery fee and the courier's share of it. `money.apply_bps`
cannot express a rate above 1, and that is deliberate.

**Surge's demand half → clinical triage.** When supply is short the queue is
re-sequenced by need, not repriced. `domain/triage.py` is that substitute, and
`ClinicalUrgency` is a column on `Medicine` set by a pharmacist, because drug
schedule cannot stand in for urgency — a rescue inhaler and a sleeping tablet
are both Schedule H.

**Airport ETR / Uber Eats prep prediction → `ready_at`.** The pickup is not
ready when the order arrives. Summoning the nearest courier immediately just
converts courier idle time into courier-waiting-at-a-counter time, inside the
same 60-minute budget. `domain/readiness.py` predicts when the counter will be
ready and `courier_summon_at` schedules backwards from it.

**Acceptance and on-time rates → routing weight, never deactivation.** See below.

## What was refused, and why

These are in the code as comments at the point of temptation, because a list in
a document does not stop anybody.

**Surge on the medicine price.** Unlawful. `applicable_cap = min(MRP, DPCO)` is
a statutory maximum and production prices exactly at it, so headroom is zero.
`test_a_ceiling_above_mrp_does_not_raise_the_price` is the anti-surge invariant.

**Clamping an over-cap price instead of raising.** The most dangerous available
refactor: it keeps every existing test green while converting a regulatory
alarm into silent normal operation. `PriceCapBreach` stays a hard error.

**Price as the rationing mechanism.** Rider abandonment under surge is Uber's
mechanism working. Demand suppression here is the harm — the conditions that
spike demand (a heatwave, dengue season, festival closures) are exactly when a
price rise lands on the people least able to absorb it.

**City-relative percentile thresholds for adverse action.** "Better than the
Jaipur average" is not an acceptable standard for dispensing the wrong drug.
Compliance thresholds are absolute.

**Rolling windows that make one bad event statistically invisible.** That
invisibility is Uber's deliberate design goal for ratings and exactly wrong for
a single wrong dispense. Reliability metrics may be amortised; dispensing
accuracy may not.

**Deactivation as the primary lever.** Uber can cut the bottom tail because the
next driver is equivalent. Removing a chemist removes the only stock of a drug
inside the radius, and the patient bears the cost of the enforcement action.

**Broadcast offers on the chemist leg.** Uber can show one trip to several
drivers because a driver is not consumed by being offered. Stock is. Offers are
sequential and hold a lease.

**At-least-once retries reaching the dispensing event unguarded.** A replayed
dispense would duplicate a statutory register row and double-debit a patient's
authorisation. `services/dispensing.py` is idempotent and the database has a
uniqueness constraint behind it.

**The push stream as a system of record.** Fine for a trip status on a phone,
unacceptable for "has this prescription been verified". The database decides
gate state.

**Ringpop geo-sharding, and the driver phone as failover store.** One city, a
few dozen fixed premises. These solve problems this system does not have, and
a statutory register may not be reconstructed from a courier's handset.

## Where the model is honestly weaker than Uber's

- **ETA is a closed-form estimate**, not a routing engine with a learned
  residual. There is no road graph and no traffic feed. The right next step is
  to *measure* prediction error against realised arrival — the data is already
  stored — before fitting anything.
- **H3 earns less here than at Uber.** Its aggregation half does not transfer
  to immobile, non-fungible chemists; it is used for retrieval and for courier
  density. For a few dozen partners a bounding box would do. It is in because
  retrieval has to scale past the pilot and because the courier side genuinely
  wants it.
- **No batching yet.** Multi-order runs and basket splitting across two
  pharmacies are the largest remaining efficiency gain, and splitting has an
  unresolved legal question (one prescription, two dispensing pharmacies).
- **No learned prep model, no marketplace simulator, no payout ledger.**

## Sources

Uber's architecture here is drawn from public material: the H3 documentation
and open-source repository, Uber's engineering blog, and secondary reporting.
It was researched for this rebuild rather than recalled, and the research
agents were asked to mark anything they could not verify. Treat specific
internal details as "publicly reported" rather than confirmed; the mechanisms
that matter to this design — the offer loop, the atomic accept, the single
consistent store, hex indexing — are the well-documented ones.
