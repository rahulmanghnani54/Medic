# Medic — 60-minute medicine delivery, Jaipur pilot

A medicine-only delivery marketplace. A customer searches for a medicine, the
platform finds the nearest partner chemist that actually has it, and a rider
delivers within 60 minutes. The platform holds no inventory and runs no dark
stores: licensed local chemists fulfil every order.

This repository implements the MVP scoped in
[`docs/venture-brief.md`](docs/venture-brief.md) — customer app, chemist
dashboard, and admin/pharmacist console — with the compliance rules enforced in
code rather than in process.

> **Pilot build.** Development authentication, placeholder catalogue data, and
> no payments. Nothing here has been reviewed by a lawyer. Read
> [`docs/compliance.md`](docs/compliance.md) before this goes anywhere near a
> real patient.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m medic.seed          # Jaipur zones, 5 chemists, 14 medicines
.venv/bin/python -m uvicorn medic.main:app --reload
```

Then open:

| | |
|---|---|
| http://127.0.0.1:8000/customer/ | search, upload a prescription, track the clock |
| http://127.0.0.1:8000/chemist/ | accept orders, confirm stock, export the H1 register |
| http://127.0.0.1:8000/admin/ | verify prescriptions, SLA board, measured economics |
| http://127.0.0.1:8000/docs | OpenAPI reference |

Tests and lint:

```bash
.venv/bin/python -m pytest        # 143 tests, ~4s
.venv/bin/ruff check medic/ tests/
```

## Try the whole journey

Place an order for Azithromycin (Schedule H1) in the customer console. It will
refuse without a prescription — upload any image, then order. The order sits at
`AWAITING_FULFILMENT`. Accept it in the chemist console: the chemist gate
clears but the order is *still held*. Verify the prescription in the admin
console and it becomes `READY_FOR_PICKUP`. Dispatch and deliver it, then look at
the chemist's H1 register CSV: one row for the antibiotic, nothing for any OTC
line in the same basket.

Then try ordering Alprazolam (Schedule X). It is refused outright, with the
medicine named, and no amount of paperwork unlocks it.

## How it is built

```
medic/domain/     the rules      — pure functions and dataclasses, no I/O
medic/services/   orchestration  — persistence and the audit trail
medic/api/        HTTP           — three routers, one per console
web/              three consoles — vanilla JS, no build step
docs/             brief, architecture, compliance, economics
```

Python 3.11+, FastAPI, SQLAlchemy 2.0, SQLite. The domain layer has no database
or HTTP dependency, which is what lets the compliance rules be tested
exhaustively in milliseconds.

See [`docs/architecture.md`](docs/architecture.md) for the design, including the
two-gate order lifecycle and the two database round-trip bugs the type layer now
prevents.

## What the platform enforces

- **Schedule-driven controls.** Every catalogue item is tagged OTC / H / H1 / X,
  and the tag alone decides whether a prescription, a pharmacist check and a
  register entry are required. Unknown or unclassified medicines **fail closed**.
- **Schedule X is refused outright**, and the refusal names the medicine so the
  patient knows what to collect in person.
- **Two concurrent gates.** A chemist confirming stock and a pharmacist
  verifying the prescription both have to clear. Running them in parallel is
  what makes 60 minutes achievable on a prescription basket.
- **Authorisations bind.** A pharmacist authorises a quantity; dispatch refuses
  to exceed it, including across repeat orders on the same prescription.
- **Re-uploaded prescriptions are flagged** to the pharmacist rather than
  silently accepted.
- **Price ceilings bind automatically.** A DPCO/NPPA-capped item is sold at its
  ceiling without anyone remembering, and that statutory reduction is reported
  separately from a chemist-funded discount.
- **Everything is audited.** Append-only, with actor, action and subject.

## On the numbers

`GET /api/admin/economics` computes contribution from *delivered orders*, not
from the brief's assumption table, which is the mechanism for the brief's own
next step: "replace the assumptions in Section 6 with real numbers."

The implemented model reproduces the brief's worked example (+₹15.50 against its
stated +₹16) and surfaces two things worth knowing before quoting it:

1. **Small orders lose money on every delivery.** A ₹150 basket contributes
   −₹8.50. The rider cost is fixed; the commission is not. This puts "OTC orders
   drive daily volume" in direct tension with "positive contribution per order".
2. **Our free-delivery threshold creates a contribution trough.** A ₹600 order
   contributes *less* than a ₹450 one, because crossing ₹500 gives up the whole
   ₹25 fee while commission rises by about ₹15.

Both are pinned by tests. See [`docs/economics.md`](docs/economics.md).

## Before launch

The short version of [`docs/compliance.md`](docs/compliance.md):

- Replace the development token auth — it is a shared static string per role.
- Prescription images are stored unencrypted on local disk.
- Build the real catalogue from partner chemists' masters and confirm every
  schedule tag. The seeded prices and tags are placeholders.
- Settle the open legal questions, starting with who is the dispenser of record.
- Replace every cost assumption in `medic/config.py` with a real Jaipur quote.
