# Venture brief: 60-minute medicine delivery, Jaipur

> This is the October 2026 venture brief that this repository implements,
> converted to Markdown from the original PDF. The verbatim text extraction is
> kept alongside it as `venture-brief.source.txt`.
>
> **How to read this document** (from the original): market facts are sourced
> from published reports and may have changed since publication. All
> unit-economics figures are illustrative assumptions, not market data, and must
> be replaced with real quotes from Jaipur chemists and delivery partners.
> Regulatory points reflect our current understanding and must be confirmed with
> an Indian pharma lawyer before launch.

## 1. Executive summary

An India-based, medicine-only delivery marketplace. Customers search for a
medicine; the app finds the nearest partner chemist that has it in stock; a
rider delivers within 60 minutes. We hold no inventory and run no dark stores.
Licensed local chemists fulfil every order, and we earn a commission plus a
delivery fee.

| Element | Decision |
|---|---|
| Model | Marketplace: partner chemists fulfil, we own the app, customer and delivery |
| Launch city | Jaipur (tier-2, less 60-minute competition than the metros) |
| Promise | Medicines delivered within 60 minutes |
| Catalogue | Medicines only, full catalogue depth (not a side category) |
| OTC / general medicines | Order directly, no prescription needed |
| Prescription-only drugs | Supplied only after prescription upload and pharmacist verification (e.g. sleeping pills, Schedule H/H1 drugs) |
| Pricing | Set by us within MRP limits; discounts funded mainly by chemists |

**Verdict:** credible as a focused, compliance-first, single-city play anchored
on chronic-care refills, with OTC orders driving daily volume. Not viable as a
head-on 10-minute speed race against the funded quick-commerce players in the
metros.

## 2. Market landscape

| Player | Speed | Where (as reported) | Notes |
|---|---|---|---|
| Zepto Pharmacy | ~10 min | Select zones: Mumbai, Delhi-NCR, Bengaluru, Hyderabad | Quick-commerce, own model |
| Blinkit | ~10 min | Pilot in parts of Bengaluru | Quick-commerce |
| Swiggy Instamart | ~10 min | Metros | Partnered with PharmEasy |
| Flipkart Minutes | ~10 min | Bengaluru, Gurugram, parts of Mumbai | Partners with local chemists |
| PhonePe Pincode | ~10 min | Bengaluru, Mumbai, Pune | Local chemist network, 24/7 |
| Farmako | ~30 min | Gurgaon, New Delhi, Moradabad | Own optimised pharmacies, 24/7 |
| DavaNinja | <60 min | Mumbai | Local chemist network |
| Plazza | ~30 min | Urban areas | Dedicated medicine player |
| PillO | 60 min | Launched Feb 2025 | Pharma e-commerce app |
| Tata 1mg, Apollo 24/7, PharmEasy, Netmeds | Same-day / 2-3 hr | Nationwide | Testing faster fulfilment |

**The gap:** nobody yet does fast medicine delivery reliably, compliantly and
with a full catalogue outside the top metros. No reports found of 10-minute
medicine delivery live in Jaipur; this needs on-the-ground verification.

Market signals worth noting:

- Quick-commerce platforms have been criticised for delivering prescription
  drugs, including antibiotics, without valid prescriptions. Strict verification
  is a positioning advantage.
- The All India Organisation of Chemists and Druggists (AIOCD) asked the home
  ministry to ban 10-minute medicine deliveries after Zepto's launch. A
  chemist-partner model aligns with chemists rather than fighting them.
- In January 2026 the Labour Minister pushed platforms to drop 10-minute
  delivery claims over rider safety. A 60-minute promise is less exposed.
- State drug regulators have shut quick-commerce stores elsewhere over licensing
  and storage. Compliance must be built in from day one.

## 3. Business model: "Zomato for medicines"

We are the marketplace layer: chemists list and fulfil, we own the customer, the
app and the delivery, and we take a commission.

**Where the analogy holds:** asset-light (no inventory or stores); revenue from
commission, delivery fees and later paid placement; network effects as more
chemists mean better stock coverage and faster delivery.

**Where it breaks, and what we build instead:**

- **Customers don't browse chemists.** They want a specific drug. The app is
  "search medicine, we match the nearest chemist that has it" — closer to Uber
  than Zomato.
- **Stock accuracy is the core tech.** A chemist may not have the strip. Live
  inventory or instant chemist confirmation is essential to the 60-minute
  promise.
- **Regulation sits inside every order:** prescription checks, drug schedules,
  H1 registers.
- **Lower take rate.** Food apps take roughly 20–25%; medicine margins can't
  support that. We estimate 8–12%, to be tested with Jaipur chemists.
- **Trust matters more.** One wrong or expired drug is a health incident, not a
  refund.

### Core product: three apps

| App | Key functions |
|---|---|
| Customer app | Medicine search, OTC checkout, prescription upload, live order tracking, refill reminders, order history |
| Chemist dashboard | Order accept/reject within a time limit, stock confirmation, prescription view, payouts, H1 register export |
| Admin / pharmacist console | Prescription verification queue, drug-schedule catalogue tagging, rider dispatch, chemist SLA monitoring, fraud flags |

## 4. Prescription and compliance model

| Drug category | Handling (to be confirmed by counsel) |
|---|---|
| OTC / general | Direct order, no prescription |
| Schedule H | Prescription upload + pharmacist check before dispatch |
| Schedule H1 (incl. many antibiotics, some sleeping and anti-anxiety drugs) | Prescription upload + pharmacist check + automatic register entry (prescriber, patient, quantity) |
| Schedule X (certain strong psychotropics and narcotics) | Likely excluded: we believe these need the original paper prescription retained by the pharmacy |

**Safeguards to build:** tag every catalogue item by drug schedule so the app
enforces the prescription rule automatically (classification is by schedule, not
by dose alone); limit how many times one prescription can be used and match
quantities to the prescription; detect repeat uploads of the same prescription
image, especially for sleeping pills; show users a clear "pharmacist verifying"
step (about 5–10 minutes) so the 60-minute promise stays credible; 24/7
pharmacist cover for the verification queue.

> **Regulatory caution.** We believe India's e-pharmacy rules are still not
> finalised. Confirm current central rules and Rajasthan state drug-licensing
> requirements with an Indian pharma lawyer before launch. Pricing cannot exceed
> MRP, and many drugs are price-capped under DPCO/NPPA; verify current rules.

## 5. Jaipur launch plan

- **Chemist supply first:** sign 30–50 partner chemists across dense zones such
  as Malviya Nagar, Vaishali Nagar, Mansarovar, C-Scheme and Raja Park.
- **Stock reliability:** require instant stock confirmation from chemists; move
  to live inventory sync with the larger partners.
- **Delivery:** start with a hyperlocal delivery partner, then add own riders in
  the densest zones.
- **Anchor customers:** chronic-care patients (diabetes, BP, thyroid, cardiac)
  on monthly refills, plus OTC everyday orders for habit.
- **Legal:** Rajasthan drug licence position, chemist partner agreements,
  pharmacist liability terms.

| Phase | Goal | Exit criterion |
|---|---|---|
| 0. Validate | Interview 20+ chemists and 50+ chronic patients; get real margin and rider quotes | Confirmed take rate and delivery cost |
| 1. Pilot | 2–3 zones, 30–50 chemists, OTC + prescription flow | Most orders delivered within 60 min; low stock-out cancellations |
| 2. City scale | All of Jaipur, refill subscriptions, chemist ads | Positive contribution per order |
| 3. Expand | Next tier-2 city in Rajasthan or nearby | Repeatable launch playbook |

## 6. Illustrative unit economics (per order)

> All figures below are assumptions, not market data. Replace them with real
> quotes from Jaipur chemists and delivery partners before using them for any
> decision or investor material.

| Line item | Assumption | ₹ per order |
|---|---|---|
| Average order value | Mixed OTC + prescription | 450 |
| Commission from chemist | ~10% of order value | +45 |
| Delivery fee from customer | Flat fee | +25 |
| Rider cost | Tier-2 city, per delivery | −40 |
| Payment gateway | ~2% | −9 |
| Pharmacist checks and support | Allocated | −5 |
| **Contribution per order** | Before discounts and marketing | **+16** |

**What this means:** margins are thin, and platform-funded discounts would make
most orders loss-making, so chemists should fund discounts from their own
margin. If fixed costs run about ₹6–8 lakh a month (team, pharmacists, tech,
office), break-even needs roughly 40,000–50,000 orders a month, about 1,500 a
day — ambitious for one tier-2 city. Levers to improve: chronic-care refills
(often ₹1,500+ order value), batched deliveries, minimum order value for free
delivery, paid extras such as teleconsults and lab-test bookings, and chemist
paid placement.

> See `economics.md` for what the implemented model says about these numbers,
> including two findings worth knowing before quoting them.

## 7. Pros and cons

| Pros | Cons / risks |
|---|---|
| Real, recurring, need-based demand | E-pharmacy regulation unsettled; rules could change quickly |
| Tier-2 cities like Jaipur are underserved | Funded giants can subsidise if they enter Jaipur |
| Asset-light chemist-partner model | Depends on chemists' stock accuracy and responsiveness |
| Compliance-first positioning vs. quick-commerce criticism | Prescription checks add 5–15 min to delivery |
| Medicine-only focus: deeper catalogue, more trust | Thin margins; small orders are costly to deliver fast |
| Aligns with chemists rather than opposing them | Liability for wrong or expired drugs |
| Expansion into refills, consults, diagnostics, ads | Pricing capped by MRP and DPCO/NPPA rules |

## 8. Next steps

- Engage an Indian pharma lawyer: e-pharmacy status, Rajasthan licensing,
  Schedule X handling, chemist agreements.
- Interview Jaipur chemists: real margins, willingness to pay 8–12% commission,
  stock-confirmation speed.
- Get delivery quotes from hyperlocal partners in Jaipur.
- Replace the assumptions in Section 6 with real numbers and rebuild the model.
- Scope the MVP: customer app, chemist dashboard, admin/pharmacist console.
- Visit 2–3 target zones to verify current fast-delivery competition on the
  ground.

## Sources

Published reports used for market facts in the original brief. Details may have
changed since publication, and these are the brief's citations rather than
independently re-verified ones.

- CXO Digital Pulse: Zepto enters 10-minute medicine delivery (cxodigitalpulse.com)
- Inc42: Blinkit piloting 10-minute medicine delivery in Bengaluru (inc42.com)
- Inc42: Quick Pharma: can Zepto, Blinkit, Instamart cope with the rules (inc42.com)
- Entrackr: Flipkart to enter 10-minute medicine delivery market (entrackr.com)
- Outlook Business: PhonePe's Pincode begins 10-minute medicine delivery (outlookbusiness.com)
- Farmako Healthcare company profile (altss.com)
- Digital Health News: DavaNinja launches 60-min medicine delivery in Mumbai (digitalhealthnews.com)
- The CapTable: Q-commerce promised what e-pharmacies couldn't (the-captable.com)
- Deccan Herald: Doctors warn of antibiotic misuse as quick-commerce apps bypass prescriptions (deccanherald.com)
- India TV News: Blinkit drops 10-minute delivery branding after government steps in (indiatvnews.com)
- Business Standard: Zepto / Blinkit topic pages, FDA licence actions (business-standard.com)
