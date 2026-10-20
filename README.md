# Medic — 60-minute medicine delivery, Jaipur pilot

A medicine-only delivery marketplace, built on **Uber's dispatch architecture**.
A customer searches for a medicine; the platform offers the order to partner
chemists in turn until one accepts; a courier collects and delivers inside 60
minutes. The platform holds no inventory and runs no dark stores.

This implements the MVP scoped in [`docs/venture-brief.md`](docs/venture-brief.md),
rebuilt on the model the brief itself pointed at — *"closer to Uber than
Zomato"* — with the compliance rules enforced in code rather than in process.

> **Pilot build.** Development authentication, placeholder catalogue data, and
> no payments. Nothing here has been reviewed by a lawyer. Read
> [`docs/compliance.md`](docs/compliance.md) before this goes near a patient.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m medic.seed              # Jaipur zones, 5 chemists, 4 couriers, 14 medicines
.venv/bin/python -m uvicorn medic.main:app --reload
.venv/bin/python -m medic.services.workers  # in a second terminal: the dispatch loop
```

| | |
|---|---|
| http://127.0.0.1:8000/customer/ | search, upload a prescription, watch the ETA band firm up |
| http://127.0.0.1:8000/chemist/ | answer offers before they expire, confirm stock, H1 register |
| http://127.0.0.1:8000/courier/ | go on shift, take delivery offers, work the run |
| http://127.0.0.1:8000/admin/ | verify prescriptions, dispatch funnel, SLA board, economics |
| http://127.0.0.1:8000/docs | OpenAPI reference |

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check medic/ tests/
```

The worker is what makes expiry and re-offering happen. Without it an
unanswered offer just sits there; the API still works, orders simply stall
after the first offer.

## The architecture, in one idea

**Uber's dispatch loop transfers almost exactly. Uber's matching objective does
not transfer at all.** Separating those two is the whole design, and
[`docs/uber-mapping.md`](docs/uber-mapping.md) is the full record of what was
taken, adapted and refused.

An order is **not** assigned at checkout:

```
place_order ─→ SEEKING_CHEMIST ─→ [offer to best candidate]
                    ↑                       │
                    └── decline/expire/withdraw
                                        accept
                                            ↓
                        AWAITING_FULFILMENT ─→ READY_FOR_PICKUP ─→ OUT_FOR_DELIVERY ─→ DELIVERED
                              (both gates)
```

- **A decline does not kill the order.** Uber's asymmetry: supply refusing is a
  routing event. Previously a chemist's refusal was terminal.
- **Accept is atomic** — win the offer, bind the chemist, lease the stock, one
  transaction. Uber itself retreated from sagas on this path.
- **Feasibility is re-checked every attempt**, against what is left of the hour,
  not against the full 60 minutes.
- **Offers are sequential, never broadcast.** A driver is not consumed by being
  offered; stock is, so an offer holds a lease.

## Try it

In the customer console, order **Azithromycin 500mg** (Schedule H1). It is
refused without a prescription; upload any image and order. The order sits in
`SEEKING_CHEMIST` with **no chemist named** — the quote gives an ETA band, not
a promise, because nobody has agreed to anything yet.

In the chemist console, **decline** the offer. The order does not die; the next
chemist is offered it. Accept there: the chemist gate clears but the order is
*still held* until a pharmacist verifies in the admin console.

Then in the courier console, go on shift and take the delivery. Completing the
**collect** stop is the dispensing event — it writes the Schedule H1 register
with your courier id as the custody holder. Try to end your shift before
handing over: a **409, custody held**.

The admin console's dispatch funnel shows the whole thing: who was asked, what
they said, and what is still looking.

## What the platform enforces

- **Schedule-driven controls.** OTC / H / H1 / X tagging alone decides whether a
  prescription, a pharmacist check and a register entry are required. Unknown
  or unclassified medicines **fail closed**. Schedule X is refused outright and
  the refusal names the medicine.
- **Two concurrent gates.** A chemist confirming stock and a pharmacist
  verifying both have to clear. Running them in parallel is what makes 60
  minutes achievable at all; the prescription gate never re-opens once decided.
- **One module owns the dispensing event.** The register write, the prescription
  debit and the physical movement of goods are one transaction, and it is
  idempotent — a replayed dispense cannot duplicate a statutory row.
- **Stock is leased, not decremented.** Releasing a lease twice is a no-op; a
  double release of a decrement would invent phantom units of a scheduled drug.
- **Custody is a fact, not a status.** Read from the courier's waypoint plan,
  recorded on every order, and it blocks a courier from going offline.
- **Price ceilings bind automatically.** DPCO/NPPA-capped items sell at the
  ceiling without anyone remembering, and there is deliberately no primitive in
  the money layer that can express a multiplier above 1.
- **Everything is audited**, append-only, through one chokepoint.

## On surge pricing

Uber clears excess demand with price. That cannot be copied here, and the
refusal is structural rather than a policy setting: `applicable_cap = min(MRP,
DPCO ceiling)` is a statutory maximum, production already prices exactly at it,
and `money.apply_bps` cannot represent a rate above 1.

The two-sided lever is replaced by three separate ones:

- a **deadline** (`domain/deadlines.py`) couples the stages;
- **clinical triage** (`domain/triage.py`) rations by need, not willingness to
  pay — when supply is short the queue is re-sequenced, never repriced;
- the **delivery fee and courier incentive**, the one genuinely elastic side.

## Geospatial

`domain/geocells.py` wraps **Uber's actual H3 library**, not a reimplementation.
Resolution 8 (~0.75 km²) is the dispatch grain, resolution 7 the zoning grain.
The grid is a retrieval optimisation and never the authority: exact haversine
distance decides serviceability, and the cell prefilter is provably a superset
of what the exact check admits.

## Layout

```
medic/domain/     the rules       — pure, no DB, no HTTP, no clock
medic/services/   orchestration   — dispatching, couriers, dispensing, stock, workers
medic/api/        HTTP            — four routers, one per console
web/              four consoles   — vanilla JS, no build step
docs/             brief, architecture, uber-mapping, compliance, economics
```

## On the numbers

`GET /api/admin/economics` measures contribution from delivered orders rather
than the brief's assumption table. The model reproduces the brief's worked
example (+₹15.50 against its stated +₹16) and surfaces two things worth knowing
before quoting it — small orders lose money on every delivery, and the
free-delivery threshold creates a contribution trough. Both are pinned by
tests. See [`docs/economics.md`](docs/economics.md).

## Before launch

See [`docs/compliance.md`](docs/compliance.md). The short version: replace the
development token auth, encrypt prescription images, build the real catalogue
and confirm every schedule tag, settle who is the dispensing pharmacist of
record under re-sourcing, and replace every cost assumption in
`medic/config.py` with a real Jaipur quote.
