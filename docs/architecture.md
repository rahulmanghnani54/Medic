# Architecture

## Shape

A marketplace, not a pharmacy. The platform holds no inventory and runs no dark
stores: licensed partner chemists fulfil every order, and the software's job is
dispatch, compliance and the clock.

It is built on Uber's dispatch model, with the parts that do not transfer
deliberately refused. See [`uber-mapping.md`](uber-mapping.md) for what was
taken, adapted and rejected, and why.

```
  Customer app  ─┐                        ┌─→ services/dispatching.py  (the offer loop)
  Chemist app   ─┼─→  FastAPI  ─→  services ┼─→ services/couriers.py    (supply side)
  Courier app   ─┤    (api/*)              ├─→ services/dispensing.py   (the register)
  Admin app     ─┘                        └─→ services/stock.py        (leases)
                                               │
                           domain/*  (rules: no DB, no HTTP, no clock)
                                               │
                                            SQLite
                      services/workers.py  ── drives the loop on a tick
```

Four layers, with a strict dependency direction:

| Layer | Holds | Knows about |
|---|---|---|
| `medic/domain/` | the rules | nothing — no database, no HTTP, no clock of its own |
| `medic/services/` | orchestration, persistence, audit | domain + models |
| `medic/api/` | HTTP shape, auth, presentation | services |
| `web/` | four consoles | the API only |

Enums live in the domain layer and `models.py` imports them, so there is no
`domain → models → domain` cycle. `from medic.models import OrderStatus` still
works, because models re-exports them.

## The dispatch loop

The central change from the first build. An order is **not** assigned at
checkout. It is priced, checked for feasibility, and entered into
`SEEKING_CHEMIST` with no chemist bound. The dispatcher then offers it to
ranked candidates one at a time.

```
  place_order ─→ SEEKING_CHEMIST ─→ [Offer to best candidate]
                      ↑                      │
                      │        decline / expire / withdraw
                      └──────────────────────┤
                                        accept
                                             ↓
                            AWAITING_FULFILMENT ─→ READY_FOR_PICKUP
                                  (both gates)
```

Three properties are load-bearing:

**A decline does not kill the order.** This is Uber's asymmetry: the supply
side refusing is a routing event. Previously a chemist's refusal was terminal.

**Accept is atomic.** Winning the offer, binding the chemist and leasing the
stock happen in one transaction. Uber itself retreated from application-layer
sagas on this path; the entity in that inconsistent window here would be a
reservation of a controlled drug.

**Feasibility is re-checked every attempt.** A candidate feasible at minute 0
is not feasible after a timed-out offer round. `matching.admits` is the single
ordered filter chain, called by both the matcher and the accept path so they
cannot drift.

Offers are **sequential, never broadcast**. Uber can show one trip to many
drivers because a driver is not consumed by being offered. Stock is: an offer
holds a `StockLease`, and two chemists leasing the same units would
double-promise a scheduled drug.

## Stock: leases, not decrements

`ChemistStock` keeps `qty_on_hand` (what is physically on the shelf) apart from
`qty_reserved` (held by open leases). Availability is the difference.

The previous design decremented on accept and incremented back on release.
That is unsafe once a crash-prone worker can release twice: the second
increment *inflates* declared stock and invents phantom units. A lease is a row
with a terminal state, so releasing one twice is a no-op rather than an
arithmetic operation.

## The two gates

An order must clear both before a rider collects:

- **chemist gate** — a licensed chemist accepted and confirmed the stock;
- **prescription gate** — a pharmacist verified, where the basket needs it.

They run **concurrently**, which is what makes 60 minutes achievable on a
prescription basket. `estimate_eta` models this as `max(prep, verification) +
travel`, not a sum.

The two gates are asymmetric and `assert_gate_transition` enforces it: the
chemist gate may regress `CLEARED → SEEKING`, because a chemist can hand an
order back, but the prescription gate never re-opens. A pharmacist's decision
is final for that order, and re-sourcing a chemist does not invalidate it.

## Custody and the dispensing event

Completing a courier's COLLECT stop **is** the dispensing event. It goes
through `services/dispensing.py` and nowhere else, so the Schedule H1 register
entry, the prescription debit and the physical movement of the goods stay in
one transaction. Goods cannot leave a pharmacy on a call that failed to record
it.

`Order.dispensed_at` records that moment for **every** order, not just
scheduled ones — an OTC basket in a courier's bag is just as much out of the
chemist's hands, and the register has no row for it.

A courier holding collected-but-not-handed-over goods cannot go offline, and
custody is read from the waypoint plan rather than the order's status column.
Once transport events arrive asynchronously, status stops being a sound proxy
for where the goods physically are.

## Couriers

The courier is the leg that genuinely behaves like Uber's driver side. They
hold an ordered `WaypointPlan` rather than a current trip, so one courier can
carry order A while collecting order B — a scalar "en route to chemist" state
could not represent that.

They authenticate **as individuals**, not by role. The register records who
took custody of a Schedule H1 package, and a fleet-wide shared secret cannot
answer that question.

## Geospatial index

`domain/geocells.py` wraps **Uber's actual H3 library**. Resolution 8 (~0.75
km², ~530 m edge) is the dispatch grain; resolution 7 (~5.2 km²) is the zoning
grain.

The grid is a **retrieval optimisation and never the authority**. Exact
haversine distance and the ETA check decide serviceability, and the cell
prefilter is deliberately over-inclusive so it can never exclude a chemist the
exact check would have admitted — a property pinned by a property test over
thousands of sampled points.

## Matching
## Matching

`domain/matching.py` answers one question: which partner chemist can fill this
exact basket, legally and actually, inside the window?

Filters, in order: active → licence not expired → open at this hour → cold chain
if needed → inside the radius → ETA leaves a safety margin → has stock for every
line. Survivors are ranked by ETA, then by the chemist's acceptance and on-time
rates.

Chemists that pass everything but stock become *partial fills*, which is how the
customer gets told "the nearest chemist is short of X" rather than a bare
failure.

Declared stock makes a chemist a candidate; it does not commit the order. Stock
is reserved only on acceptance and released if the chemist later withdraws.

## Money

Integers in paise, everywhere. `domain/money.py` is the only place that converts.
Rounding is half-up and explicit, because a rounding error against a DPCO
ceiling is a compliance problem rather than a cosmetic one.

## Timestamps

All UTC and all timezone-aware, enforced by the `UtcDateTime` column type in
`medic/models.py`. SQLite has no native `timestamptz`: a plain
`DateTime(timezone=True)` hands back **naive** datetimes, which would make every
SLA calculation either raise or silently mis-compute. `tests/test_lifecycle.py`
guards the round trip.

## Enum columns

`enum_column()` in `medic/models.py`, not `String`. A `Mapped[OrderStatus]` on a
plain string column returns a bare `str` after a database round trip, so
`order.status is OrderStatus.X` quietly evaluates `False` — which reads as
"nothing is blocking this order" rather than raising. Also guarded by a test.

## What is deliberately not here

See `docs/compliance.md` for the full list. The big ones: real authentication,
payments, a rider app, routed distances, and splitting a basket across two
chemists.
