"""FastAPI application.

Three consoles are served from one app for the pilot: /customer, /chemist and
/admin. Splitting them out is a deployment change, not a rewrite, because they
share nothing but the API.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from medic.api import admin, chemist, customer
from medic.config import settings
from medic.db import create_all
from medic.domain.orders import IllegalTransition
from medic.domain.prescriptions import PrescriptionProblem
from medic.domain.pricing import CommissionOutOfBand, PriceCapBreach
from medic.domain.schedules import NotDispensableOnline
from medic.services.ordering import OrderingError

log = logging.getLogger("medic")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    create_all()
    if settings.using_default_tokens():
        log.warning(
            "Running with the built-in development tokens. Set MEDIC_TOKEN_* before "
            "exposing this to anything but localhost."
        )
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Medic -- 60-minute medicine delivery (Jaipur pilot)",
    version="0.1.0",
    description=(
        "Marketplace API. Partner chemists fulfil every order; the platform holds no "
        "inventory. See docs/compliance.md for the regulatory questions that are still open."
    ),
)


def _problem(status_code: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": kind, "detail": message})


@app.exception_handler(NotDispensableOnline)
def handle_not_dispensable(_: Request, exc: NotDispensableOnline) -> JSONResponse:
    # 451: we are refusing on legal grounds rather than because the request is malformed.
    return _problem(451, str(exc), "not_dispensable_online")


@app.exception_handler(PrescriptionProblem)
def handle_prescription(_: Request, exc: PrescriptionProblem) -> JSONResponse:
    return _problem(422, str(exc), "prescription_problem")


@app.exception_handler(PriceCapBreach)
def handle_price_cap(_: Request, exc: PriceCapBreach) -> JSONResponse:
    return _problem(409, str(exc), "price_cap_breach")


@app.exception_handler(CommissionOutOfBand)
def handle_commission(_: Request, exc: CommissionOutOfBand) -> JSONResponse:
    return _problem(409, str(exc), "commission_out_of_band")


@app.exception_handler(IllegalTransition)
def handle_transition(_: Request, exc: IllegalTransition) -> JSONResponse:
    return _problem(409, str(exc), "illegal_transition")


@app.exception_handler(OrderingError)
def handle_ordering(_: Request, exc: OrderingError) -> JSONResponse:
    return _problem(400, str(exc), "ordering_error")


app.include_router(customer.router)
app.include_router(chemist.router)
app.include_router(admin.router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


for console in ("customer", "chemist", "admin"):
    app.mount(
        f"/{console}",
        StaticFiles(directory=os.path.join(WEB_DIR, console), html=True),
        name=console,
    )
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")
