"""HTTP API contract tests (FastAPI TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seatlab.api.app import create_app

SMALL = {"cap": 120, "days": 2, "seed": 5}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.4.0"


def test_meta_documents_defaults_and_mechanisms(client: TestClient) -> None:
    body = client.get("/api/v1/meta").json()
    assert body["defaults"]["cap"] == 500
    assert set(body["policies"]) == {
        "fcfs",
        "t30",
        "t15",
        "smart",
        "patrol",
        "res",
        "mix",
        "credit",
        "price",
    }
    assert body["algorithm"]["day_minutes"] == 840
    assert body["limits"]["max_capacity"] == 5000


def test_simulate_returns_typed_metrics(client: TestClient) -> None:
    response = client.post(
        "/api/v1/simulate",
        json={"params": SMALL, "policy": {"key": "smart"}, "demand_scale": 1.4},
    )
    assert response.status_code == 200
    body = response.json()
    metrics = body["metrics"]
    assert metrics["key"] == "smart"
    assert 0 <= metrics["served_rate"] <= 1
    assert metrics["welfare_day"] > 0
    assert set(body["welfare_breakdown"]) == {
        "study_value_day",
        "wait_cost_day",
        "monitor_day",
        "welfare_day",
    }


def test_simulate_is_idempotent(client: TestClient) -> None:
    payload = {"params": SMALL, "policy": {"key": "t30"}, "demand_scale": 1.3}
    first = client.post("/api/v1/simulate", json=payload).json()
    second = client.post("/api/v1/simulate", json=payload).json()
    assert first == second


def test_simulate_rejects_bad_payloads(client: TestClient) -> None:
    assert client.post("/api/v1/simulate", json={"params": {"cap": 5}}).status_code == 422
    assert client.post("/api/v1/simulate", json={"params": {"nope": 1}}).status_code == 422
    assert client.post("/api/v1/simulate", json={"policy": {"key": "foo"}}).status_code == 422
    assert client.post("/api/v1/simulate", json={"policy": {"release": 0}}).status_code == 422
    assert client.post("/api/v1/simulate", json={"demand_scale": 9}).status_code == 422


def test_engine_errors_surface_as_422(client: TestClient) -> None:
    """Valid schema, impossible model: stay longer than the library is open."""
    response = client.post(
        "/api/v1/simulate",
        json={"params": {"cap": 120, "days": 1, "open_minutes": 600, "mean_present_h": 12.5}},
    )
    assert response.status_code == 422
    assert "mean_present_h" in response.json()["detail"]


def test_compare_reports_gains_against_the_first_policy(client: TestClient) -> None:
    body = client.post(
        "/api/v1/compare",
        json={"params": SMALL, "policies": ["fcfs", "smart", "price"], "demand_scale": 1.4},
    ).json()
    assert [r["key"] for r in body["rows"]] == ["fcfs", "smart", "price"]
    assert body["base_key"] == "fcfs"
    assert body["welfare_gain_pct"]["fcfs"] == pytest.approx(0.0, abs=1e-9)
    assert body["best_welfare_key"] in {"fcfs", "smart", "price"}
    assert all(
        r["arrivals_by_hour"] == body["rows"][0]["arrivals_by_hour"] for r in body["rows"][:2]
    )


def test_optimize_runs_the_requested_grid(client: TestClient) -> None:
    body = client.post(
        "/api/v1/optimize",
        json={"params": SMALL, "days": 2, "release_values": [20, 30], "trigger_values": [0.9]},
    ).json()
    assert len(body["rows"]) == 2
    assert body["best_welfare"]["welfare_day"] == max(r["welfare_day"] for r in body["rows"])
    assert body["days"] == 2


def test_optimize_rejects_an_oversized_grid(client: TestClient) -> None:
    response = client.post(
        "/api/v1/optimize",
        json={"params": SMALL, "release_values": list(range(20, 41)), "trigger_values": [0.5] * 10},
    )
    assert response.status_code == 422


def test_pricing_curve(client: TestClient) -> None:
    body = client.post(
        "/api/v1/pricing", json={"params": SMALL, "days": 2, "prices": [0, 1, 2]}
    ).json()
    assert [r["price"] for r in body["rows"]] == [0.0, 1.0, 2.0]
    assert body["best"]["welfare_day"] == max(r["welfare_day"] for r in body["rows"])
    assert body["gain_vs_free_pct"] is not None


def test_scale_up(client: TestClient) -> None:
    body = client.post("/api/v1/scale-up", json={}).json()
    assert body["wasted_hours_yi"] == pytest.approx([14.98, 20.97], abs=0.05)
    assert body["seats_wan"][0] > 0


def test_api_key_is_enforced_when_configured() -> None:
    guarded = TestClient(create_app(api_key="secret"))
    assert guarded.get("/healthz").status_code == 200
    assert guarded.post("/api/v1/simulate", json={"params": SMALL}).status_code == 401
    ok = guarded.post("/api/v1/simulate", json={"params": SMALL}, headers={"X-API-Key": "secret"})
    assert ok.status_code == 200


def test_static_pages_are_served(client: TestClient) -> None:
    landing = client.get("/")
    assert landing.status_code == 200
    assert "SeatLab" in landing.text
    lab = client.get("/lab")
    assert lab.status_code == 200
    assert len(lab.text) > 80_000
    assert client.get("/static/index.html").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "SeatLab API"


def test_metrics_endpoint_is_prometheus_text(client: TestClient) -> None:
    client.get("/api/v1/meta")
    body = client.get("/metrics").text
    assert "seatlab_requests_total" in body
    assert "# TYPE seatlab_engine_seconds_total counter" in body
    assert 'seatlab_requests_by_path{path="/api/v1/meta"}' in body


def test_observability_headers_and_request_id(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    assert float(r.headers["X-SeatLab-Runtime-Ms"]) >= 0
    r2 = client.get("/healthz")
    assert len(r2.headers["X-Request-ID"]) >= 8


def test_static_design_page_is_served(client: TestClient) -> None:
    r = client.get("/design")
    assert r.status_code == 200
    assert "机制设计器" in r.text
    assert "/api/v1/simulate" in r.text


def test_calibrate_recovers_a_synthetic_truth(client: TestClient) -> None:
    truth = {
        "days": 5,
        "seed": 889,
        "mean_present_h": 5.0,
        "hoard_mu": 1.5,
        "visits_per_seat": 1.5,
    }
    observed = client.post("/api/v1/simulate", json={"params": truth, "demand_scale": 1.25}).json()[
        "metrics"
    ]
    r = client.post(
        "/api/v1/calibrate",
        json={
            "params": {"days": 5, "seed": 889},
            "demand_scale": 1.25,
            "days": 5,
            "targets": {
                "arrivals_per_day": observed["arrivals"],
                "empty_share": observed["empty_share"],
                "mean_study_h": observed["avg_study_h"],
            },
        },
    )
    assert r.status_code == 200, r.text
    fit = r.json()["params"]
    assert abs(fit["visits_per_seat"] - 1.5) / 1.5 < 0.2
    assert abs(fit["mean_present_h"] - 5.0) / 5.0 < 0.2
    assert abs(r.json()["residuals"]["mean_study_h_abs"]) < 0.1
    assert r.json()["saturated"] == []


def test_sweep_returns_a_grid_and_csv(client: TestClient) -> None:
    r = client.post(
        "/api/v1/sweep",
        json={"caps": [250, 400], "demands": [1.2], "days": 6},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 2
    assert max(body["rows"], key=lambda c: c["welfare_day"]) == body["best"]
    csv = client.post(
        "/api/v1/sweep?format=csv",
        json={"caps": [250, 400], "demands": [1.2], "days": 6},
    )
    assert csv.status_code == 200
    assert csv.text.splitlines()[0].startswith("cap,demand")


def test_bad_api_key_is_rejected_with_hmac_compare() -> None:
    guarded = TestClient(create_app(api_key="secret"))
    assert (
        guarded.post("/api/v1/simulate", json={}, headers={"X-API-Key": "wrong"}).status_code == 401
    )
    assert (
        guarded.post("/api/v1/simulate", json={}, headers={"X-API-Key": "secret"}).status_code
        == 200
    )
