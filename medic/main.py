"""FastAPI application.

Three consoles are served from one app for the pilot: /customer, /chemist and
/admin. Splitting them out is a deployment change, not a rewrite, because they
share nothing but the API.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from medic.api import admin, chemist, courier, customer
from medic.config import settings
from medic.db import create_all
from medic.domain.couriers import (
    CustodyHeld,
    IllegalCourierTransition,
    IllegalStopTransition,
)
from medic.domain.dispatch import IllegalOfferTransition
from medic.domain.orders import IllegalGateTransition, IllegalTransition, SourcingExhausted
from medic.domain.prescriptions import PrescriptionProblem
from medic.domain.pricing import CommissionOutOfBand, PriceCapBreach
from medic.domain.schedules import NotDispensableOnline
from medic.services import workers
from medic.services.couriers import CourierError
from medic.services.dispatching import DispatchError
from medic.services.dispensing import DispensingRefused
from medic.services.ordering import OrderingError
from medic.services.stock import InsufficientStock

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

    # Optionally host the dispatch loop in this process. Without a loop running
    # somewhere, offers never expire and an order stalls after its first one,
    # so a single-process mode is the difference between "the app runs" and
    # "the app works" when you only have one terminal.
    stop = threading.Event()
    worker: threading.Thread | None = None
    if settings.run_worker_in_process:
        worker = threading.Thread(
            target=workers.run_forever,
            kwargs={"interval_seconds": settings.worker_tick_seconds, "stop": stop},
            name="medic-dispatch",
            daemon=True,
        )
        worker.start()
        log.warning(
            "Dispatch loop running in this process, ticking every %.1fs.",
            settings.worker_tick_seconds,
        )
    else:
        # Logged at WARNING deliberately. Without a loop running somewhere,
        # offers never expire and every order stalls after its first one --
        # the app will look like it works and quietly will not.
        log.warning(
            "Dispatch loop is NOT running in this process. Offers will never expire. "
            "Run `python -m medic.services.workers` alongside this, or restart with "
            "MEDIC_RUN_WORKER=1."
        )
    app.state.worker_thread = worker

    yield

    stop.set()
    if worker is not None:
        worker.join(timeout=settings.worker_tick_seconds + 2)


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


@app.exception_handler(DispatchError)
def handle_dispatch(_: Request, exc: DispatchError) -> JSONResponse:
    return _problem(409, str(exc), "dispatch_error")


@app.exception_handler(CourierError)
def handle_courier(_: Request, exc: CourierError) -> JSONResponse:
    return _problem(409, str(exc), "courier_error")


@app.exception_handler(CustodyHeld)
def handle_custody(_: Request, exc: CustodyHeld) -> JSONResponse:
    # A courier holding dispensed goods cannot be released. This is a custody
    # rule, so it is a conflict with the current state, not a bad request.
    return _problem(409, str(exc), "custody_held")


@app.exception_handler(DispensingRefused)
def handle_dispensing(_: Request, exc: DispensingRefused) -> JSONResponse:
    return _problem(409, str(exc), "dispensing_refused")


@app.exception_handler(InsufficientStock)
def handle_stock(_: Request, exc: InsufficientStock) -> JSONResponse:
    return _problem(409, str(exc), "insufficient_stock")


@app.exception_handler(IllegalOfferTransition)
def handle_offer_transition(_: Request, exc: IllegalOfferTransition) -> JSONResponse:
    return _problem(409, str(exc), "illegal_offer_transition")


@app.exception_handler(IllegalGateTransition)
def handle_gate_transition(_: Request, exc: IllegalGateTransition) -> JSONResponse:
    return _problem(409, str(exc), "illegal_gate_transition")


@app.exception_handler(IllegalCourierTransition)
def handle_courier_transition(_: Request, exc: IllegalCourierTransition) -> JSONResponse:
    return _problem(409, str(exc), "illegal_courier_transition")


@app.exception_handler(IllegalStopTransition)
def handle_stop_transition(_: Request, exc: IllegalStopTransition) -> JSONResponse:
    return _problem(409, str(exc), "illegal_stop_transition")


@app.exception_handler(SourcingExhausted)
def handle_sourcing_exhausted(_: Request, exc: SourcingExhausted) -> JSONResponse:
    return _problem(409, str(exc), "sourcing_exhausted")


app.include_router(customer.router)
app.include_router(chemist.router)
app.include_router(admin.router)
app.include_router(courier.router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, object]:
    """Liveness, plus whether the dispatch loop is actually running.

    Worth reporting: a server with no loop answers every request correctly
    and still never advances an order past its first offer, which is the
    hardest kind of broken to notice.
    """
    worker = getattr(app.state, "worker_thread", None)
    return {
        "status": "ok",
        "dispatch_loop_in_process": bool(worker is not None and worker.is_alive()),
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


for console in ("customer", "chemist", "admin", "courier"):
    app.mount(
        f"/{console}",
        StaticFiles(directory=os.path.join(WEB_DIR, console), html=True),
        name=console,
    )
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")
