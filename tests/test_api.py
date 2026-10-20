"""HTTP surface: roles, the three consoles and the error contract."""

from __future__ import annotations

from sqlalchemy import select

from medic.models import Medicine


def medicine_id(session, name) -> int:
    return session.scalars(select(Medicine).where(Medicine.name == name)).one().id


MALVIYA = {"delivery_lat": 26.8505, "delivery_lng": 75.8060}


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


def test_a_quote_shows_the_eta_and_the_promise(client, headers, seeded):
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
    assert 0 < body["eta_minutes"] <= 60
    assert body["requires_prescription"] is False
    assert body["chemist_name"]


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
    assert body["status"] == "AWAITING_FULFILMENT"
    assert body["prescription_gate"] == "NOT_REQUIRED"
    assert body["minutes_remaining"] <= 60
    assert "confirming stock" in body["customer_message"]


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
    placed = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    ).json()
    response = client.get(
        f"/api/customer/orders/{placed['id']}",
        params={"customer_id": 2},
        headers=headers["customer"],
    )
    assert response.status_code == 404


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

    # Customer places the order; it waits at the verification gate.
    order = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": azithro, "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            "prescription_id": prescription_id,
            **MALVIYA,
        },
        headers=headers["customer"],
    ).json()
    assert order["prescription_gate"] == "PENDING"
    assert "pharmacist is verifying" in order["customer_message"]
    chemist_id = order["chemist_id"]

    # Chemist sees it and confirms stock.
    queue = client.get(
        f"/api/chemist/{chemist_id}/orders", headers=headers["chemist"]
    ).json()
    assert [row["id"] for row in queue] == [order["id"]]
    accepted = client.post(
        f"/api/chemist/{chemist_id}/orders/{order['id']}/accept", headers=headers["chemist"]
    ).json()
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
    tracked = client.get(
        f"/api/customer/orders/{order['id']}",
        params={"customer_id": 1},
        headers=headers["customer"],
    ).json()
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
    order = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    ).json()
    chemist_id = order["chemist_id"]
    client.post(
        f"/api/chemist/{chemist_id}/orders/{order['id']}/accept", headers=headers["chemist"]
    )
    client.post(
        f"/api/admin/orders/{order['id']}/dispatch",
        json={"rider": "rider-7"},
        headers=headers["admin"],
    )
    register = client.get(
        f"/api/chemist/{chemist_id}/register.csv", headers=headers["chemist"]
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


def test_a_stock_out_rejection_tells_the_customer_they_are_not_charged(client, headers, seeded):
    order = client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    ).json()
    rejected = client.post(
        f"/api/chemist/{order['chemist_id']}/orders/{order['id']}/reject",
        json={"reason": "shelf is empty", "stock_out": True},
        headers=headers["chemist"],
    ).json()
    assert rejected["status"] == "CANCELLED_STOCK_OUT"
    assert "not been charged" in rejected["customer_message"]


# --- Admin console --------------------------------------------------------


def test_the_sla_board_lists_live_orders_and_what_they_wait_on(client, headers, seeded):
    client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    board = client.get("/api/admin/sla", headers=headers["admin"]).json()
    assert len(board) == 1
    assert board[0]["waiting_on"] == "chemist to confirm stock"
    assert board[0]["breached"] is False


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
    client.post(
        "/api/customer/orders",
        json={
            "customer_id": 1,
            "lines": [{"medicine_id": medicine_id(seeded, "Paracetamol 650mg"), "qty": 1}],
            "delivery_zone": "Malviya Nagar",
            **MALVIYA,
        },
        headers=headers["customer"],
    )
    events = client.get("/api/admin/audit", headers=headers["admin"]).json()
    assert events[0]["action"] == "order.placed"
    assert events[0]["actor"] == "customer:1"


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
