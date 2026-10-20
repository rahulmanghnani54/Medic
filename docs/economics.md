# Unit economics

The brief's section 6 is explicit that its figures are **assumptions, not market
data**, and must be replaced with real Jaipur quotes. This document records what
the code does with them, and two things the model surfaced that are worth
knowing before anyone quotes a number at an investor.

## Where the assumptions live

All of them are in `medic/config.py`, in one dataclass, in paise:

| Assumption | Value | Source |
|---|---|---|
| Commission band | 800–1200 bps (8–12%) | brief, to be tested with chemists |
| Default commission | 1000 bps (10%) | brief |
| Delivery fee | ₹25 flat | brief |
| Free delivery above | ₹500 | our addition, see the trough below |
| Rider cost | ₹40 | brief |
| Payment gateway | 200 bps (2%) | brief |
| Pharmacist cost per order | ₹5 | brief |
| Monthly fixed cost | ₹7,00,000 | midpoint of the brief's ₹6–8 lakh |

Changing any of them breaks `test_the_models_contribution_matches_the_brief_at_its_stated_aov`,
which is the point: the business case was argued on those numbers, so moving one
should be a deliberate act with a visible diff.

## The model reproduces the brief

At the brief's ₹450 average order value with 10% commission:

| Line | Model | Brief |
|---|---|---|
| Commission | +₹45.00 | +₹45 |
| Delivery fee | +₹25.00 | +₹25 |
| Rider | −₹40.00 | −₹40 |
| Payment gateway | −₹9.50 | −₹9 |
| Pharmacist | −₹5.00 | −₹5 |
| **Contribution** | **+₹15.50** | **+₹16** |
| Break-even | ~45,000 orders/month | 40,000–50,000 |

The 50 paise difference is deliberate: we charge the gateway fee on the amount
actually collected (order **plus** delivery fee), not on order value alone.

## Finding 1: the brief's conclusion holds, and it is harsh

Contribution by order value, at 10% commission:

| Order value | Contribution | Break-even |
|---|---|---|
| ₹150 | **−₹8.50** | never |
| ₹203 | **−₹4.26** | never |
| ₹300 | +₹3.50 | ~200,000/month |
| ₹450 | +₹15.50 | ~45,000/month |
| ₹600 | +₹3.00 | ~233,000/month |
| ₹1,500 | +₹75.00 | ~9,300/month |

A small OTC order **loses money on every delivery**. The ₹40 rider is fixed
while the ₹10-ish commission is not. This is not a modelling artefact; it is the
arithmetic of the brief's own assumptions, and it is why the brief anchors on
chronic-care refills.

The practical consequence for the pilot: "OTC orders drive daily volume" and
"the pilot must reach positive contribution per order" are in tension. Volume
bought with small baskets makes the contribution line worse, not better. Either
the minimum order value rises, deliveries batch, or the OTC-habit strategy is
funded explicitly as customer acquisition rather than counted as contribution.

## Finding 2: the free-delivery threshold creates a trough

A ₹600 order contributes **less** (+₹3.00) than a ₹450 one (+₹15.50). Crossing
the ₹500 free-delivery threshold gives up the whole ₹25 fee while commission
rises by only about ₹15. Contribution does not recover to its ₹450 level until
roughly ₹900.

This is our design flaw, not the brief's — the threshold is our addition. It is
pinned by `test_the_free_delivery_threshold_creates_a_contribution_trough` so it
cannot be forgotten. Options, none chosen yet because they need real margin data:
raise the threshold well past the trough, taper the fee instead of cliff-edging
it, or scale commission with basket size.

## Measuring instead of assuming

`GET /api/admin/economics` computes contribution from **delivered orders**
rather than from the assumption table. Revenue lines (commission, delivery fee)
are measured; cost lines (rider, gateway, pharmacist) are still configured,
because we have no quotes yet. The endpoint says so in its own response.

This is the mechanism for the brief's next step, "replace the assumptions in
Section 6 with real numbers and rebuild the model": as pilot orders accumulate,
the measured figure diverges from the assumed one and the gap is visible in the
admin console rather than in a spreadsheet nobody reopens.

## What is still missing from the model

- Marketing and customer-acquisition cost: not modelled at all.
- Rider idle time and batching: the ₹40 is per delivery, flat.
- Returns, refunds and failed deliveries: no cost attached.
- Chemist-side cost of participating (their time accepting orders).
- Cold-chain handling, which plausibly costs more than ₹40 to deliver.
