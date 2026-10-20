# Compliance model and open questions

**This document is not legal advice, and nothing here has been reviewed by a
lawyer.** It records what the platform currently enforces and, more importantly,
what has to be settled before a single real order is taken. The venture brief
(`docs/venture-brief.md`, section 4) marks its own regulatory handling "to be
confirmed by counsel"; this is the engineering-side version of that caveat.

## What the code enforces today

The policy lives in one place, `medic/domain/schedules.py`, so that it can be
reviewed as a single artefact and changed in a single commit.

| Schedule | Dispensable online | Prescription | Pharmacist check | Register entry |
|---|---|---|---|---|
| OTC / general | yes | no | no | no |
| H | yes | yes | yes | no |
| H1 | yes | yes | yes | **yes** |
| X | **no** | — | — | — |
| anything unclassified | **no** | — | — | — |

Design rules that follow from that table:

- **Fail closed.** `policy_for()` raises `NotDispensableOnline` for an unknown
  schedule rather than defaulting to OTC. A medicine that has not been
  classified cannot be sold.
- **Controls are a union.** One Schedule H1 line puts the whole basket through
  verification (`controls_for_basket`).
- **Paperwork is checked before stock.** `place_order` validates the basket and
  its prescription before it looks for a chemist, so a patient is told "you need
  a prescription for X", not "nobody has stock".
- **Two independent gates.** An order needs both a chemist who has confirmed
  stock and, where relevant, a pharmacist who has verified the prescription. The
  gates run concurrently, which is what makes 60 minutes achievable on a
  prescription basket; neither alone releases the order.
- **The authorisation binds at dispatch.** A pharmacist authorises a quantity,
  and `dispatch()` refuses to dispense beyond it — including across repeat
  orders on the same prescription.
- **The register is written at pickup, not delivery**, because that is when the
  medicine leaves the pharmacy's custody.
- **Price ceilings bind automatically.** A DPCO/NPPA-capped item is priced at
  its ceiling without anyone having to remember; see `docs/economics.md`.
- **Everything is audited.** `AuditEvent` is append-only and records actor,
  action and subject for every state change.

## Safeguards from the brief, and their current strength

| Safeguard | Status | Honest assessment |
|---|---|---|
| Tag every item by schedule | done | Only as good as the tagging. Seed data is placeholder; the real catalogue must be built from partner chemists' masters and each tag confirmed. |
| Limit prescription reuse | done | `max_uses` (default 3) plus per-medicine quantity tracking. |
| Detect repeat uploads | **weak** | Exact SHA-256 match only. A re-photographed or lightly cropped image will not collide. Perceptual hashing or OCR is deliberately out of MVP scope. |
| Show a "pharmacist verifying" step | done | Surfaced in the customer app and counted into the ETA. |
| 24/7 pharmacist cover | **not built** | An operational commitment, not a code change. The queue exists; staffing it does not. |

## Open questions for counsel

These block launch, not the build:

1. **E-pharmacy status.** We believe the central rules were still unsettled when
   the brief was written. What is in force now, and does a chemist-partner
   marketplace of this shape need its own registration, or does it ride on the
   partner pharmacies' licences?
2. **Rajasthan state licensing.** What does the state drug authority require of
   the platform itself, as distinct from the chemists?
3. **Schedule X.** We exclude it on the belief that the original paper
   prescription must be retained by the dispensing pharmacy. Confirm, and
   confirm whether any other category needs the same treatment.
4. **Who is the dispenser of record?** The partner chemist's pharmacist, or
   ours? This decides where liability sits for a wrong or expired medicine, and
   whether our verification step is a legal act or a commercial one.
5. **Register ownership.** We generate the Schedule H1 register per chemist on
   the theory that the obligation sits with the dispensing pharmacy. Confirm the
   required fields, retention period and format.
6. **Prescription validity window.** We default to 90 days. Is there a statutory
   period, and does it differ by schedule?
7. **Teleconsultation.** Other platforms issue a prescription via a doctor call
   after the order is placed. We deliberately do **not** do this. If it is ever
   proposed, it needs its own legal review — this is the practice that drew
   criticism for the quick-commerce entrants.
8. **Data protection.** Prescriptions are health data. Retention, access and
   breach obligations need to be specified before we hold real ones.

## Known engineering gaps

Deliberate MVP limitations, listed so nobody mistakes them for finished work:

- **Authentication is a development stub.** `medic/auth.py` is a shared static
  token per role. No user identity, no sessions, no rotation, no rate limiting.
  It must be replaced (OTP for patients, staff accounts for chemists, SSO for
  the pharmacist console) before any real data.
- **Prescription images are stored unencrypted on local disk**, keyed by content
  hash, with no access control beyond the role token. Not fit for real health
  data.
- **No payments.** Orders compute a total; nothing is charged, so there is no
  refund path for a cancelled order either.
- **No rider app or live tracking.** Dispatch and delivery are marked by hand in
  the admin console.
- **Distances are straight-line** with a 1.4× road factor, not routed.
- **Single-chemist fulfilment only.** A basket no one chemist can fill is
  refused; splitting across two is not implemented.
- **No notifications.** No SMS or push at any step.
