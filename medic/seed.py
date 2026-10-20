"""Pilot seed data for the Jaipur launch zones.

The catalogue is a small, representative sample for development: a few chronic-care
refills (the brief's anchor customer), everyday OTC lines, and scheduled drugs that
exercise each branch of the prescription rules, including a Schedule X item that the
platform must refuse.

Prices and schedule tags here are PLACEHOLDERS for development. Before the pilot,
the catalogue has to be built from the partner chemists' own master and each item's
schedule confirmed against the current classification -- getting a schedule tag wrong
is a compliance failure, not a data-quality nit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import settings
from medic.domain import geocells
from medic.domain.money import rupees
from medic.domain.schedules import DrugSchedule
from medic.domain.triage import ClinicalUrgency
from medic.models import Chemist, ChemistStock, Courier, Customer, Medicine

# Approximate centres of the launch zones named in the brief.
ZONES = {
    "Malviya Nagar": (26.8505, 75.8060),
    "Vaishali Nagar": (26.9124, 75.7400),
    "Mansarovar": (26.8520, 75.7640),
    "C-Scheme": (26.9065, 75.7985),
    "Raja Park": (26.8955, 75.8280),
}

MEDICINES = [
    # name, composition, maker, pack, form, schedule, mrp, dpco ceiling, chronic, cold, urgency
    ("Metformin 500mg", "Metformin HCl 500mg", "Generic Pharma", "strip of 15", "tablet",
     DrugSchedule.OTC, 42.0, 38.0, True, False, ClinicalUrgency.ROUTINE),
    ("Telmisartan 40mg", "Telmisartan 40mg", "Generic Pharma", "strip of 15", "tablet",
     DrugSchedule.OTC, 118.0, 105.0, True, False, ClinicalUrgency.ROUTINE),
    ("Thyroxine 50mcg", "Levothyroxine 50mcg", "Generic Pharma", "strip of 30", "tablet",
     DrugSchedule.OTC, 165.0, None, True, False, ClinicalUrgency.ROUTINE),
    ("Atorvastatin 10mg", "Atorvastatin 10mg", "Generic Pharma", "strip of 15", "tablet",
     DrugSchedule.OTC, 96.0, 88.0, True, False, ClinicalUrgency.ROUTINE),
    ("Paracetamol 650mg", "Paracetamol 650mg", "Generic Pharma", "strip of 15", "tablet",
     DrugSchedule.OTC, 30.0, 28.0, False, False, ClinicalUrgency.ELECTIVE),
    ("ORS Sachet", "Oral rehydration salts", "Generic Pharma", "sachet", "powder",
     DrugSchedule.OTC, 22.0, None, False, False, ClinicalUrgency.TIME_SENSITIVE),
    ("Antacid Suspension", "Magnesium/aluminium hydroxide", "Generic Pharma", "170ml", "syrup",
     DrugSchedule.OTC, 145.0, None, False, False, ClinicalUrgency.ELECTIVE),
    ("Cetirizine 10mg", "Cetirizine 10mg", "Generic Pharma", "strip of 10", "tablet",
     DrugSchedule.H, 35.0, None, False, False, ClinicalUrgency.ELECTIVE),
    ("Pantoprazole 40mg", "Pantoprazole 40mg", "Generic Pharma", "strip of 15", "tablet",
     DrugSchedule.H, 155.0, 140.0, False, False, ClinicalUrgency.ROUTINE),
    ("Azithromycin 500mg", "Azithromycin 500mg", "Generic Pharma", "strip of 3", "tablet",
     DrugSchedule.H1, 115.0, 102.0, False, False, ClinicalUrgency.TIME_SENSITIVE),
    ("Amoxicillin-Clav 625mg", "Amoxicillin 500mg + Clavulanic acid 125mg", "Generic Pharma",
     "strip of 10", "tablet", DrugSchedule.H1, 240.0, 215.0, False, False,
     ClinicalUrgency.TIME_SENSITIVE),
    ("Zolpidem 5mg", "Zolpidem tartrate 5mg", "Generic Pharma", "strip of 10", "tablet",
     DrugSchedule.H1, 88.0, None, False, False, ClinicalUrgency.ELECTIVE),
    ("Insulin Glargine 100IU", "Insulin glargine 100IU/ml", "Generic Pharma", "3ml pen",
     "injection", DrugSchedule.H, 780.0, None, True, True, ClinicalUrgency.CRITICAL),
    # Present so the refusal path is exercised and visible in the catalogue console.
    ("Alprazolam 0.5mg", "Alprazolam 0.5mg", "Generic Pharma", "strip of 10", "tablet",
     DrugSchedule.X, 65.0, None, False, False, ClinicalUrgency.ROUTINE),
]

CHEMISTS = [
    # name, licence, pharmacist, reg no, zone, phone, 24x7, open, close, commission bps, cold chain
    ("Shree Krishna Medicos", "RJ/JPR/20-1142", "Rakesh Sharma", "RSP-18822", "Malviya Nagar",
     "+919800000101", True, 0, 24, 1000, True),
    ("Vaishali Medical Store", "RJ/JPR/20-2277", "Neha Agarwal", "RSP-20451", "Vaishali Nagar",
     "+919800000102", False, 8, 22, 1100, False),
    ("Mansarovar Chemists", "RJ/JPR/21-0918", "Imran Khan", "RSP-19930", "Mansarovar",
     "+919800000103", False, 9, 23, 900, True),
    ("C-Scheme Pharmacy", "RJ/JPR/19-7781", "Priya Jain", "RSP-17645", "C-Scheme",
     "+919800000104", True, 0, 24, 1200, False),
    ("Raja Park Drug House", "RJ/JPR/22-3310", "Sunil Meena", "RSP-21188", "Raja Park",
     "+919800000105", False, 8, 21, 800, False),
]

# Couriers carry a per-person token, not a shared role token: the register
# has to record WHICH courier took custody of a Schedule H1 package, and a
# shared secret cannot answer that. Hashed, as a real deployment would.
COURIERS = [
    # name, phone, dev token, vehicle, cold chain
    ("Ramesh Yadav", "+919700000201", "dev-courier-1", "two-wheeler", True),
    ("Sunita Rathore", "+919700000202", "dev-courier-2", "two-wheeler", False),
    ("Arif Khan", "+919700000203", "dev-courier-3", "two-wheeler", False),
    ("Deepak Saini", "+919700000204", "dev-courier-4", "bicycle", False),
]

CUSTOMERS = [
    ("Anita Verma", "+919812345678", "Malviya Nagar"),
    ("Mahesh Gupta", "+919812345679", "Mansarovar"),
    ("Farah Siddiqui", "+919812345680", "C-Scheme"),
]


def seed(session: Session, *, reset: bool = False) -> dict[str, int]:
    """Idempotent seed. Safe to call on an existing database."""
    if reset:
        for model in (ChemistStock, Chemist, Medicine, Customer, Courier):
            session.query(model).delete()
        session.flush()

    if session.scalars(select(Medicine).limit(1)).first() is not None and not reset:
        return {"medicines": 0, "chemists": 0, "customers": 0, "stock": 0, "skipped": 1}

    medicines: list[Medicine] = []
    for (
        name, composition, maker, pack, form, schedule, mrp, ceiling, chronic, cold, urgency
    ) in MEDICINES:
        medicine = Medicine(
            name=name,
            composition=composition,
            manufacturer=maker,
            pack=pack,
            form=form,
            schedule=schedule,
            mrp_paise=rupees(mrp),
            dpco_ceiling_paise=rupees(ceiling) if ceiling else None,
            requires_cold_chain=cold,
            is_chronic_care=chronic,
            urgency=urgency,
        )
        session.add(medicine)
        medicines.append(medicine)

    chemists: list[Chemist] = []
    for (
        name, licence, pharmacist, reg_no, zone, phone, is_24x7, opens, closes, bps, cold
    ) in CHEMISTS:
        lat, lng = ZONES[zone]
        chemist = Chemist(
            name=name,
            drug_licence_no=licence,
            licence_expires_on=datetime(2027, 3, 31, tzinfo=UTC),
            pharmacist_name=pharmacist,
            pharmacist_reg_no=reg_no,
            zone=zone,
            lat=lat,
            lng=lng,
            phone=phone,
            is_active=True,
            is_24x7=is_24x7,
            opens_hour_ist=opens,
            closes_hour_ist=closes,
            commission_bps=bps,
            can_cold_chain=cold,
        )
        session.add(chemist)
        chemists.append(chemist)

    for name, phone, token, vehicle, cold in COURIERS:
        lat, lng = ZONES["Malviya Nagar"]
        session.add(
            Courier(
                name=name,
                phone=phone,
                token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                vehicle=vehicle,
                can_cold_chain=cold,
                is_active=True,
                last_lat=lat,
                last_lng=lng,
                last_cell=geocells.cell_for(lat, lng),
            )
        )

    for name, phone, zone in CUSTOMERS:
        lat, lng = ZONES[zone]
        session.add(Customer(name=name, phone=phone, zone=zone, lat=lat, lng=lng))

    session.flush()

    # Stock: every chemist carries the OTC and chronic-care lines; scheduled lines
    # are patchier, which is what makes stock-outs and partial fills realistic.
    stock_rows = 0
    for index, chemist in enumerate(chemists):
        for position, medicine in enumerate(medicines):
            if medicine.schedule == DrugSchedule.X:
                continue
            if medicine.requires_cold_chain and not chemist.can_cold_chain:
                continue
            # The first partner is the flagship 24x7 store and carries full depth,
            # which keeps the catalogue reachable. The rest are deliberately
            # patchy on scheduled lines, so stock-outs and partial fills show up
            # in development the way they will in Jaipur.
            if (
                index > 0
                and medicine.schedule in (DrugSchedule.H, DrugSchedule.H1)
                and (index + position) % 3 == 0
            ):
                continue  # this chemist happens not to carry it
            session.add(
                ChemistStock(
                    chemist_id=chemist.id,
                    medicine_id=medicine.id,
                    qty_on_hand=20 if medicine.schedule == DrugSchedule.OTC else 8,
                    source="declared",
                )
            )
            stock_rows += 1

    session.flush()
    return {
        "medicines": len(medicines),
        "chemists": len(chemists),
        "customers": len(CUSTOMERS),
        "couriers": len(COURIERS),
        "stock": stock_rows,
        "skipped": 0,
    }


def main() -> None:
    from medic.db import create_all, session_scope

    create_all()
    with session_scope() as session:
        counts = seed(session)
    print(f"Seeded {settings.database_url}: {counts}")


if __name__ == "__main__":
    main()
