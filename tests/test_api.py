"""HTTP surface: roles, the three consoles and the error contract."""

from __future__ import annotations

from sqlalchemy import select

from medic.models import Medicine


def medicine_id(session, name) -> int:
    return session.scalars(select(Medicine).where(Medicine.name == name)).one().id


MALVIYA = {"delivery_lat": 26.8505, "delivery_lng": 75.8060}

#: Midway between the two 24x7 partners, so at least two chemists are open and
#: inside the match radius whatever hour the suite runs at. Used by every test
#: that turns on a refusal being RE-OFFERED: at Malviya Nagar the 24x7 flagship
#: is often the only open candidate, and declining the only candidate
#: legitimately exhausts sourcing instead.
TONK_ROAD = {"delivery_lat": 26.8785, "delivery_lng": 75.8023}

#: The seeded partner chemists. Which of them holds an order's offer depends on
#: who is open at the hour the suite runs, so tests find the offer rather than
#: assume its offeree.
CHEMIST_IDS = (1, 2, 3, 4, 5)


def place(
    client,
    headers,
    seeded,
    name: str = "Paracetamol 650mg",
    *,
    near: dict = MALVIYA,
    zone: str = "Malviya Nagar",
    prescription_id: int | None = None,
) -> dict:
    """Place a one-line order over HTTP and return the tracking body."""
    body = {
        "customer_id": 1,
        "lines": [{"medicine_id": medicine_id(seeded, name), "qty": 1}],
        "delivery_zone": zone,
        **near,
    }
    if prescription_id is not None:
        body["prescription_id"] = prescription_id
    response = client.post("/api/customer/orders", json=body, headers=headers["customer"])
    assert response.status_code == 201
    return response.json()


def offer_for(client, headers, order_id: int) -> dict | None:
    """The open offer for an order, read off the chemist consoles.

    Which chemist holds it depends on who is open at the hour the suite runs,
    so this asks every console rather than assuming one. At most one offer is
    ever open for an order: an offer leases stock, so dispatch never broadcasts.
    """
    rows = [
        row
        for chemist_id in CHEMIST_IDS
        for row in client.get(
            f"/api/chemist/{chemist_id}/offers", headers=headers["chemist"]
        ).json()
        if row["order_id"] == order_id
    ]
    assert len(rows) <= 1
    return rows[0] if rows else None


def accept(client, headers, offer: dict) -> dict:
    response = client.post(
        f"/api/chemist/offers/{offer['offer_id']}/accept",
        params={"chemist_id": offer["chemist_id"]},
        headers=headers["chemist"],
    )
    assert response.status_code == 200
    return response.json()


def track(client, headers, order_id: int) -> dict:
    return client.get(
        f"/api/customer/orders/{order_id}",
        params={"customer_id": 1},
        headers=headers["customer"],
    ).json()


# --- Auth -----------------------------------------------------------------


def test_an_unauthenticated_request_is_rejected(client):
    assert client.get("/api/customer/medicines").status_code == 401


def test_a_wrong_role_token_is_rejected(client, headers):
    response = client.get("/api/customer/medicines", headers=headers["admin"])
    assert response.status_code == 403
    assert "customer role" in response.json()["detail"]


def test_a_customer_cannot_reach_the_pharmacist_queue(client, headers):
    response = client.get("/api/admin/verification-queue", headers=headers["customer"])
    assert response.status_code == 403


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# --- Catalogue ------------------------------------------------------------


def test_search_returns_the_prescription_flag(client, headers):
    response = client.get(
        "/api/customer/medicines", params={"q": "Azithromycin"}, headers=headers["customer"]
    )
    assert response.status_code == 200
    item = response.json()[0]
    assert item["schedule"] == "H1"
    assert item["requires_prescription"] is True


def test_search_matches_on_composition(client, headers):
    response = client.get(
        "/api/customer/medicines", params={"q": "Metformin"}, headers=headers["customer"]
    )
    assert any("Metformin" in item["name"] for item in response.json())


# --- Quote and checkout ---------------------------------------------------


def test_a_quote_shows_the_eta_band_and_the_promise(client, headers, seeded):
    response = client.post(
        "/api/customer/quote",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["promise_minutes"] == 60
    assert body["feasible"] is True
    assert body["candidate_count"] >= 1
    assert 0 < body["eta_minutes_low"] <= body["eta_minutes_high"] <= 60
    assert body["requires_prescription"] is False


def test_a_quote_names_no_chemist(client, headers, seeded):
    """The absence is the invariant: nobody has agreed to anything yet.

    Naming a chemist, or stating one ETA instead of a band, would announce a
    commitment the platform has not obtained from anybody.
    """
    body = client.post(
        "/api/customer/quote",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            **MALVIYA,
        },
        headers=headers["customer"],
    ).json()
    assert "chemist_id" not in body
    assert "chemist_name" not in body
    assert "eta_minutes" not in body


def test_placing_an_otc_order_returns_a_tracking_body(client, headers, seeded):
    response = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SEEKING_CHEMIST"
    assert body["prescription_gate"] == "NOT_REQUIRED"
    assert body["urgency"] == "ELECTIVE"
    assert body["minutes_remaining"] <= 60
    assert "Finding a partner chemist" in body["customer_message"]


def test_a_placed_order_names_no_chemist_until_one_accepts(client, headers, seeded):
    """Checkout carries no assignment, so it carries no firm promise either."""
    body = place(client, headers, seeded)
    assert body["chemist_id"] is None
    assert body["chemist_name"] is None
    assert body["committed_eta_minutes"] is None
    assert body["sourcing_attempts"] == 1  # the first offer goes out at once


def test_a_schedule_x_order_is_refused_with_451_and_an_explanation(client, headers, seeded):
    """Refused on legal grounds, and the customer is told what to do instead."""
    response = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Alprazolam 0.5mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 451
    assert response.json()["error"] == "not_dispensable_online"
    assert "Schedule X" in response.json()["detail"]


def test_a_missing_prescription_is_a_400_naming_the_medicine(client, headers, seeded):
    response = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Azithromycin 500mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 400
    assert "Azithromycin 500mg" in response.json()["detail"]


def test_a_zero_quantity_line_is_rejected_by_validation(client, headers, seeded):
    response = client.post(
        "/api/customer/quote",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 0}],
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 422


def test_tracking_someone_elses_order_returns_404(client, headers, seeded):
    placed = place(client, headers, seeded)
    response = client.get(
        f"/api/customer/orders/{placed['id']}",
        params={"customer_id": 2},
        headers=headers["customer"],
    )
    assert response.status_code == 404


# --- The offer loop on the chemist console --------------------------------


def test_one_chemist_at_a_time_holds_an_open_offer_with_a_countdown(client, headers, seeded):
    """An offer leases stock, so it goes to one chemist and carries a timer."""
    order = place(client, headers, seeded)
    offer = offer_for(client, headers, order["id"])
    assert offer is not None
    assert offer["state"] == "OFFERED"
    assert offer["attempt_seq"] == 1
    assert offer["eta_minutes"] > 0
    assert offer["expires_in_seconds"] > 0


def test_a_chemist_holding_no_offer_for_an_order_cannot_accept_it(client, headers, seeded):
    """Why acceptance is keyed on the offer rather than on the order.

    An order id used to be enough, so any chemist who could guess one could
    accept it. An offer names its offeree, so a chemist who was never asked is
    refused and the order stays in sourcing.
    """
    order = place(client, headers, seeded)
    offer = offer_for(client, headers, order["id"])
    intruder = next(cid for cid in CHEMIST_IDS if cid != offer["chemist_id"])
    response = client.post(
        f"/api/chemist/offers/{offer['offer_id']}/accept",
        params={"chemist_id": intruder},
        headers=headers["chemist"],
    )
    assert response.status_code == 409
    assert response.json()["error"] == "dispatch_error"
    assert "not made to you" in response.json()["detail"]
    tracked = track(client, headers, order["id"])
    assert tracked["status"] == "SEEKING_CHEMIST"
    assert tracked["chemist_id"] is None


def test_accepting_an_offer_binds_the_chemist_and_commits_an_eta(client, headers, seeded):
    order = place(client, headers, seeded)
    offer = offer_for(client, headers, order["id"])
    accepted = accept(client, headers, offer)
    assert accepted["chemist_gate"] == "CLEARED"
    assert accepted["chemist_id"] == offer["chemist_id"]
    assert accepted["committed_eta_minutes"] is not None
    assert accepted["status"] == "READY_FOR_PICKUP"  # OTC: both gates clear at once


def test_a_chemist_who_cannot_cover_the_basket_is_refused_at_accept(client, headers, seeded):
    """The shelf can move while an offer is open, and then nothing is bound."""
    order = place(client, headers, seeded)
    offer = offer_for(client, headers, order["id"])
    client.put(
        f"/api/chemist/{offer['chemist_id']}/stock",
        json=[
            {
                "medicine_id": medicine_id(seeded, "Paracetamol 650mg"),
                "qty_on_hand": 0,
                "source": "synced",
            }
        ],
        headers=headers["chemist"],
    )
    response = client.post(
        f"/api/chemist/offers/{offer['offer_id']}/accept",
        params={"chemist_id": offer["chemist_id"]},
        headers=headers["chemist"],
    )
    assert response.status_code == 409
    assert response.json()["error"] == "insufficient_stock"
    tracked = track(client, headers, order["id"])
    assert tracked["status"] == "SEEKING_CHEMIST"
    assert tracked["chemist_id"] is None


def test_a_chemist_declining_for_stock_out_does_not_kill_the_order(client, headers, seeded):
    """The asymmetry on the HTTP surface: supply refusing is a routing event."""
    order = place(client, headers, seeded, near=TONK_ROAD, zone="Tonk Road")
    first = offer_for(client, headers, order["id"])
    declined = client.post(
        f"/api/chemist/offers/{first['offer_id']}/decline",
        params={"chemist_id": first["chemist_id"]},
        json={"reason": "shelf is empty", "stock_out": True},
        headers=headers["chemist"],
    )
    assert declined.status_code == 200
    assert declined.json()["status"] == "SEEKING_CHEMIST"
    second = offer_for(client, headers, order["id"])
    assert second is not None
    assert second["chemist_id"] != first["chemist_id"]
    assert second["attempt_seq"] == first["attempt_seq"] + 1


def test_a_chemist_who_accepted_can_hand_the_order_back(client, headers, seeded):
    """An accept is a claim about declared stock; shelf truth comes later."""
    order = place(client, headers, seeded, near=TONK_ROAD, zone="Tonk Road")
    offer = offer_for(client, headers, order["id"])
    accept(client, headers, offer)
    withdrawn = client.post(
        f"/api/chemist/{offer['chemist_id']}/orders/{order['id']}/withdraw",
        json={"reason": "strip missing from the shelf", "stock_out": True},
        headers=headers["chemist"],
    )
    assert withdrawn.status_code == 200
    body = withdrawn.json()
    assert body["status"] == "SEEKING_CHEMIST"
    assert body["chemist_id"] is None
    assert body["committed_eta_minutes"] is None


def test_a_stock_out_that_exhausts_sourcing_tells_the_customer_they_are_not_charged(
    client, headers, seeded
):
    """The money reassurance has to survive the move to an offer loop.

    A stock-out used to close the order as CANCELLED_STOCK_OUT, whose customer
    message states that nothing has been charged. A stock-out is now a routing
    event, so the only closure left for one is running out of candidates --
    and that is where the customer has to be told.
    """
    order = place(client, headers, seeded)
    for _ in range(8):
        offer = offer_for(client, headers, order["id"])
        if offer is None:
            break
        client.post(
            f"/api/chemist/offers/{offer['offer_id']}/decline",
            params={"chemist_id": offer["chemist_id"]},
            json={"reason": "shelf is empty", "stock_out": True},
            headers=headers["chemist"],
        )
    tracked = track(client, headers, order["id"])
    assert tracked["status"] == "CANCELLED_NO_CHEMIST"
    assert tracked["closed_reason"]
    assert "not been charged" in tracked["customer_message"]


# --- Full journey across all three consoles -------------------------------


def test_a_prescription_order_crosses_all_three_consoles(client, headers, seeded):
    azithro = medicine_id(seeded, "Azithromycin 500mg")

    # Customer uploads a prescription.
    upload = client.post(
        "/api/customer/prescriptions",
        data={"customer_id": 1, "prescriber_name": "Dr A Sharma",
              "prescriber_reg_no": "RMC-44120"},
        files={"file": ("rx.jpg", b"scanned-prescription", "image/jpeg")},
        headers=headers["customer"],
    )
    assert upload.status_code == 201
    prescription_id = upload.json()["id"]
    assert upload.json()["status"] == "PENDING"

    # Customer places the order; sourcing and verification run side by side.
    order = place(client, headers, seeded, "Azithromycin 500mg", prescription_id=prescription_id)
    assert order["status"] == "SEEKING_CHEMIST"
    assert order["prescription_gate"] == "PENDING"
    assert "pharmacist is verifying" in order["customer_message"]

    # The first offer is already out. The chemist holding it sees it and accepts.
    offer = offer_for(client, headers, order["id"])
    assert offer is not None
    chemist_id = offer["chemist_id"]
    queue = client.get(
        f"/api/chemist/{chemist_id}/orders", headers=headers["chemist"]
    ).json()
    assert [row["id"] for row in queue] == [order["id"]]
    accepted = accept(client, headers, offer)
    assert accepted["chemist_gate"] == "CLEARED"
    assert accepted["status"] == "AWAITING_FULFILMENT"  # still waiting on the pharmacist

    # Pharmacist sees the order's clock in their queue, and verifies.
    pending = client.get(
        "/api/admin/verification-queue", headers=headers["pharmacist"]
    ).json()
    assert pending[0]["prescription"]["id"] == prescription_id
    assert pending[0]["orders_waiting"][0]["order_id"] == order["id"]
    assert pending[0]["is_reupload"] is False

    verified = client.post(
        f"/api/admin/prescriptions/{prescription_id}/verify",
        json={
            "authorisations": [{"medicine_id": azithro, "authorised_qty": 1}],
            "pharmacist": "Ph. R Nair",
        },
        headers=headers["pharmacist"],
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "VERIFIED"

    # Both gates clear, so the order is ready.
    tracked = track(client, headers, order["id"])
    assert tracked["status"] == "READY_FOR_PICKUP"

    # Admin dispatches and delivers.
    assert client.post(
        f"/api/admin/orders/{order['id']}/dispatch",
        json={"rider": "rider-7"},
        headers=headers["admin"],
    ).json()["status"] == "OUT_FOR_DELIVERY"
    delivered = client.post(
        f"/api/admin/orders/{order['id']}/deliver", headers=headers["admin"]
    ).json()
    assert delivered["status"] == "DELIVERED"
    assert delivered["promise_honoured"] is True

    # The H1 register now has the dispensing entry.
    register = client.get(
        f"/api/chemist/{chemist_id}/register.csv", headers=headers["chemist"]
    )
    assert register.status_code == 200
    rows = register.text.strip().splitlines()
    assert len(rows) == 2
    assert "Azithromycin 500mg" in rows[1]
    assert "RMC-44120" in rows[1]


def test_an_otc_order_leaves_the_register_empty(client, headers, seeded):
    order = place(client, headers, seeded)
    offer = offer_for(client, headers, order["id"])
    accepted = accept(client, headers, offer)
    assert accepted["status"] == "READY_FOR_PICKUP"
    dispatched = client.post(
        f"/api/admin/orders/{order['id']}/dispatch",
        json={"rider": "rider-7"},
        headers=headers["admin"],
    ).json()
    assert dispatched["status"] == "OUT_FOR_DELIVERY"
    register = client.get(
        f"/api/chemist/{offer['chemist_id']}/register.csv", headers=headers["chemist"]
    )
    assert len(register.text.strip().splitlines()) == 1  # header only


def test_a_reupload_is_flagged_in_the_pharmacist_queue(client, headers):
    for _ in range(2):
        client.post(
            "/api/customer/prescriptions",
            data={"customer_id": 1},
            files={"file": ("rx.jpg", b"identical-bytes", "image/jpeg")},
            headers=headers["customer"],
        )
    queue = client.get("/api/admin/verification-queue", headers=headers["pharmacist"]).json()
    assert [row["is_reupload"] for row in queue] == [False, True]


def test_an_empty_prescription_file_is_rejected(client, headers):
    response = client.post(
        "/api/customer/prescriptions",
        data={"customer_id": 1},
        files={"file": ("rx.jpg", b"", "image/jpeg")},
        headers=headers["customer"],
    )
    assert response.status_code == 400


# --- Chemist dashboard ----------------------------------------------------


def test_a_chemist_can_declare_stock_and_read_it_back(client, headers, seeded):
    paracetamol = medicine_id(seeded, "Paracetamol 650mg")
    written = client.put(
        "/api/chemist/1/stock",
        json=[{"medicine_id": paracetamol, "qty_on_hand": 3, "source": "synced"}],
        headers=headers["chemist"],
    )
    assert written.json() == {"rows_written": 1}
    rows = client.get("/api/chemist/1/stock", headers=headers["chemist"]).json()
    row = next(r for r in rows if r["medicine_id"] == paracetamol)
    assert row["qty_on_hand"] == 3
    assert row["source"] == "synced"


def test_declaring_stock_for_an_unknown_medicine_is_rejected(client, headers):
    response = client.put(
        "/api/chemist/1/stock",
        json=[{"medicine_id": 999_999, "qty_on_hand": 1}],
        headers=headers["chemist"],
    )
    assert response.status_code == 400


def test_an_unknown_chemist_is_a_404(client, headers):
    assert client.get("/api/chemist/999/orders", headers=headers["chemist"]).status_code == 404


# --- Admin console --------------------------------------------------------


def test_the_sla_board_lists_live_orders_and_what_they_wait_on(client, headers, seeded):
    """An order being sourced is live: its clock runs with no chemist bound."""
    place(client, headers, seeded)
    board = client.get("/api/admin/sla", headers=headers["admin"]).json()
    assert len(board) == 1
    assert board[0]["status"] == "SEEKING_CHEMIST"
    assert board[0]["waiting_on"] == "a chemist to accept"
    assert board[0]["breached"] is False


def test_the_dispatch_board_shows_the_offer_funnel(client, headers, seeded):
    """The diagnostic the synchronous design could not produce: who was asked."""
    order = place(client, headers, seeded)
    board = client.get("/api/admin/dispatch", headers=headers["admin"]).json()
    assert board["offers_by_state"]["OFFERED"] == 1
    assert [row["order_id"] for row in board["open_offers"]] == [order["id"]]
    assert [row["order_id"] for row in board["seeking_chemist"]] == [order["id"]]


def test_the_economics_endpoint_reports_zero_before_any_delivery(client, headers):
    body = client.get("/api/admin/economics", headers=headers["admin"]).json()
    assert body["delivered_orders"] == 0
    assert body["break_even_orders_per_month"] is None
    assert "assumptions" in body["assumptions_note"]


def test_the_catalogue_console_exposes_the_schedule_tagging(client, headers):
    rows = client.get("/api/admin/catalogue", headers=headers["admin"]).json()
    by_name = {row["name"]: row for row in rows}
    assert by_name["Alprazolam 0.5mg"]["schedule"] == "X"
    assert by_name["Azithromycin 500mg"]["requires_register_entry"] is True
    assert by_name["Paracetamol 650mg"]["requires_prescription"] is False


def test_the_audit_log_is_readable(client, headers, seeded):
    place(client, headers, seeded)
    events = client.get("/api/admin/audit", headers=headers["admin"]).json()
    # Newest first, and placing an order makes the first offer at once, so the
    # dispatcher's event sits on top of the customer's.
    assert events[0]["action"] == "offer.made"
    assert events[0]["actor"] == "dispatcher"
    placed = next(event for event in events if event["action"] == "order.placed")
    assert placed["actor"] == "customer:1"


def test_the_three_consoles_are_served(client):
    for path in ("/customer/", "/chemist/", "/admin/"):
        assert client.get(path).status_code == 200


def test_a_schedule_x_refusal_names_the_medicine(client, headers, seeded):
    """A refusal the patient reads has to say which item to collect in person."""
    response = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [
                {"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1},
                {"medicine_id": medicine_id(seeded, "Alprazolam 0.5mg"), "qty": 1},
            ],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    assert response.status_code == 451
    assert "Alprazolam 0.5mg" in response.json()["detail"]
