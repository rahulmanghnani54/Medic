# Architecture

## Shape

A marketplace, not a pharmacy. The platform holds no inventory and runs no dark
stores: licensed partner chemists fulfil every order, and the software's job is
matching, compliance and the clock.

```
  Customer app  ─┐
  Chemist app   ─┼─→  FastAPI  ─→  services/ordering.py  ─→  domain/*  ─→  SQLite
  Admin app     ─┘    (api/*)      (orchestration,           (rules,
                                    audit trail)              no I/O)
```

Four layers, with a strict dependency direction:

| Layer | Holds | Knows about |
|---|---|---|
| `medic/domain/` | the rules | nothing — no database, no HTTP, no clock of its own |
| `medic/services/` | orchestration, persistence, audit | domain + models |
| `medic/api/` | HTTP shape, auth, presentation | services |
| `web/` | three consoles | the API only |

The domain layer takes plain dataclasses and returns plain dataclasses. That is
what lets the compliance rules be tested exhaustively without a database, and it
is why `tests/test_schedules.py` and `tests/test_pricing.py` run in
milliseconds.

## The two gates

The central design decision. An order must clear both before a rider collects:

- **chemist gate** — a licensed chemist accepted it and confirmed the stock;
- **prescription gate** — a pharmacist verified the prescription, where the
  basket needs one (`NOT_REQUIRED` when it does not).

They are tracked separately on the order and run **concurrently**. That is what
makes 60 minutes achievable on a prescription basket: the pharmacist verifies
while the chemist packs. `estimate_eta` models this as
`max(prep, verification) + travel`, not a sum.

```
CREATED ─→ AWAITING_FULFILMENT ─→ READY_FOR_PICKUP ─→ OUT_FOR_DELIVERY ─→ DELIVERED
               │   (both gates)                                 
               ├─→ CANCELLED_STOCK_OUT        ├─→ CANCELLED_STOCK_OUT
               ├─→ REJECTED_PRESCRIPTION      └─→ CANCELLED_BY_CUSTOMER
               ├─→ CANCELLED_NO_CHEMIST
               └─→ CANCELLED_BY_CUSTOMER
```

Failure states are kept distinct rather than collapsed into one `CANCELLED`,
because the pilot's exit criteria are stated per cause: "most orders delivered
within 60 min, low stock-out cancellations" needs stock-outs separable from
prescription rejections.

Once a rider holds the goods the customer can no longer cancel — scheduled drugs
have to come back to the dispensing chemist, which is a support process, not a
button.

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
