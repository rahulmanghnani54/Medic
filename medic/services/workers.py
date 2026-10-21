"""The dispatcher's host process.

An offer loop needs something to drive it. Without a ticking worker, an offer
that nobody answers simply sits there: the expiry time is written on the row,
but nothing notices it has passed, and the order waits out its hour in
silence. This module is the loop that notices.

Everything it calls is idempotent and safe to retry, because a worker that
can crash is a worker that will replay. It is deliberately a single process:
Uber shards its dispatch tier across a gossip ring because it runs a global
mobile fleet, and adopting that here would add failure modes to a one-city
pilot without removing any.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import Settings, settings
from medic.db import session_scope
from medic.domain.orders import OrderStatus
from medic.domain.triage import dispatch_priority
from medic.models import Order
from medic.services import couriers as courier_service
from medic.services import dispatching

log = logging.getLogger("medic.workers")


@dataclass
class TickReport:
    """What one pass of the loop did. Returned so tests can assert on it."""

    expired_chemist_offers: int = 0
    expired_courier_offers: int = 0
    sourcing_advanced: int = 0
    orders_exhausted: int = 0
    couriers_offered: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return bool(
            self.expired_chemist_offers
            or self.expired_courier_offers
            or self.sourcing_advanced
            or self.couriers_offered
        )


def _live_orders(session: Session, status: OrderStatus) -> list[Order]:
    """Live orders in one state, most clinically urgent first.

    Sequencing by need is the lawful substitute for surge's demand-rationing
    half: when supply is short we re-order the queue, we never reprice.
    """
    orders = list(session.scalars(select(Order).where(Order.status == status)))
    return sorted(orders, key=lambda o: (dispatch_priority(o.urgency), o.created_at))


def tick(
    session: Session, *, now: datetime | None = None, config: Settings = settings
) -> TickReport:
    """One pass of the dispatch loop. Idempotent.

    Ordered so that each step feeds the next within the same tick: expire
    first, so an order freed by an expiry is re-offered immediately rather
    than waiting a whole interval.
    """
    now = now or datetime.now(UTC)
    report = TickReport()

    report.expired_chemist_offers = len(dispatching.expire_due_offers(session, now=now))
    report.expired_courier_offers = len(
        courier_service.expire_due_courier_offers(session, now=now)
    )

    for order in _live_orders(session, OrderStatus.SEEKING_CHEMIST):
        try:
            outcome = dispatching.advance_sourcing(
                session, order_id=order.id, now=now, config=config
            )
        except Exception as exc:  # noqa: BLE001 - one bad order must not stop the loop
            report.errors.append(f"order {order.id}: {exc}")
            log.warning("sourcing failed for order %s: %s", order.id, exc)
            continue
        if outcome.action == "offered":
            report.sourcing_advanced += 1
        elif outcome.action == "exhausted":
            report.orders_exhausted += 1

    for order in _live_orders(session, OrderStatus.READY_FOR_PICKUP):
        try:
            if courier_service.offer_delivery(
                session, order_id=order.id, now=now, config=config
            ):
                report.couriers_offered += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"courier offer for order {order.id}: {exc}")
            log.warning("courier offer failed for order %s: %s", order.id, exc)

    return report


def run_forever(
    *,
    interval_seconds: float = 5.0,
    config: Settings = settings,
    stop: threading.Event | None = None,
) -> None:
    """Drive the loop until stopped.

    Each tick opens its own transaction: a failure in one pass must not leave
    the next one working inside a poisoned session. `stop` lets a host process
    shut the loop down cleanly instead of relying on the thread being a
    daemon.
    """
    log.info("dispatch worker started, tick every %.1fs", interval_seconds)
    while not (stop is not None and stop.is_set()):
        try:
            with session_scope() as session:
                report = tick(session, config=config)
            if report.did_work:
                log.info(
                    "tick: expired=%d/%d sourced=%d exhausted=%d courier_offers=%d",
                    report.expired_chemist_offers,
                    report.expired_courier_offers,
                    report.sourcing_advanced,
                    report.orders_exhausted,
                    report.couriers_offered,
                )
            for problem in report.errors:
                log.warning("tick problem: %s", problem)
        except Exception:  # noqa: BLE001 - the loop outlives any single failure
            log.exception("dispatch tick failed")
        if stop is not None:
            # Waiting on the event rather than sleeping means shutdown is
            # immediate instead of up to one tick late.
            stop.wait(interval_seconds)
        else:
            time.sleep(interval_seconds)
    log.info("dispatch worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
