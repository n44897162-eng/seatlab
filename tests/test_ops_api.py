"""HTTP tests for the control plane and the student app."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from seatlab.api.app import create_app

ADMIN = {"X-Admin-Key": "adm"}
APP = {"X-App-Key": "stu", "X-Student-ID": "20241001"}


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 21, 9, 0)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, minutes: int) -> None:
        self.at += timedelta(minutes=minutes)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def client(clock: Clock) -> TestClient:
    return TestClient(create_app(admin_key="adm", app_key="stu", now=clock))


def test_admin_and_app_auth_are_separate(client: TestClient) -> None:
    assert client.get("/api/v1/admin/dashboard").status_code == 401
    assert client.get("/api/v1/admin/dashboard", headers=APP).status_code == 401
    assert client.get("/api/v1/admin/dashboard", headers=ADMIN).status_code == 200
    assert client.post("/api/v1/app/reservations", json={"student_id": "S1"}).status_code == 401
    assert client.get("/api/v1/app/availability").status_code == 200


def test_venue_patch_validates_and_rebuilds(client: TestClient) -> None:
    assert client.get("/api/v1/admin/venue", headers=ADMIN).json()["cap"] == 500
    bad = client.patch("/api/v1/admin/venue", headers=ADMIN, json={"cap": 5})
    assert bad.status_code == 422
    ok = client.patch("/api/v1/admin/venue", headers=ADMIN, json={"cap": 120, "name": "测试馆"})
    assert ok.status_code == 200 and ok.json()["cap"] == 120
    seats = client.get("/api/v1/admin/seats", headers=ADMIN).json()["seats"]
    assert len(seats) == 120


def test_policy_activation_and_plan(client: TestClient) -> None:
    bad = client.post("/api/v1/admin/policies/activate", headers=ADMIN, json={"key": "nope"})
    assert bad.status_code == 422
    good = client.post(
        "/api/v1/admin/policies/activate",
        headers=ADMIN,
        json={"key": "t15", "overrides": {"release": 15, "trigger": 0}},
    )
    assert good.status_code == 200 and good.json()["policy"]["release"] == 15
    plan = client.post("/api/v1/admin/plan", headers=ADMIN, json={"demands": [1.0, 1.4], "days": 3})
    assert plan.status_code == 200
    assert len(plan.json()["rows"]) == 6


def test_student_lifecycle_over_http(client: TestClient, clock: Clock) -> None:
    r = client.post("/api/v1/app/reservations", headers=APP, json={"zone": "A", "minutes": 120})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "reserved"
    assert len(res["code"]) == 4 and res["code"].isdigit()

    wrong = client.post(
        f"/api/v1/app/reservations/{res['id']}/checkin",
        headers=APP,
        json={"student_id": "20241001", "code": "0000" if res["code"] != "0000" else "1111"},
    )
    assert wrong.status_code == 403

    ok = client.post(
        f"/api/v1/app/reservations/{res['id']}/checkin",
        headers=APP,
        json={"student_id": "20241001", "code": res["code"]},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "checked_in"

    away = client.post(
        f"/api/v1/app/reservations/{res['id']}/away",
        headers=APP,
        json={"student_id": "20241001", "minutes": 15},
    )
    assert away.status_code == 200 and away.json()["status"] == "away"

    back = client.post(
        f"/api/v1/app/reservations/{res['id']}/back",
        headers=APP,
        json={"student_id": "20241001"},
    )
    assert back.status_code == 200

    other = client.post(
        f"/api/v1/app/reservations/{res['id']}/back",
        headers={**APP, "X-Student-ID": "someone-else"},
        json={"student_id": "someone-else"},
    )
    assert other.status_code == 403

    me = client.get("/api/v1/app/me?student_id=20241001", headers=APP).json()
    assert me["active"][0]["status"] == "checked_in"

    gone = client.delete(f"/api/v1/app/reservations/{res['id']}?student_id=20241001", headers=APP)
    assert gone.status_code == 200 and gone.json()["status"] == "released"


def test_expired_reservation_is_reported_as_410(client: TestClient, clock: Clock) -> None:
    res = client.post("/api/v1/app/reservations", headers=APP, json={"zone": "A"}).json()
    clock.advance(20)
    late = client.post(
        f"/api/v1/app/reservations/{res['id']}/checkin",
        headers=APP,
        json={"student_id": "20241001", "code": res["code"]},
    )
    assert late.status_code == 409
    assert late.json()["detail"].startswith("reservation is expired")


def test_complaint_and_violation_workflow(client: TestClient) -> None:
    res = client.post(
        "/api/v1/app/reservations",
        headers={**APP, "X-Student-ID": "holder"},
        json={"student_id": "holder", "zone": "A"},
    ).json()
    report = client.post(
        "/api/v1/app/reports",
        headers={**APP, "X-Student-ID": "reporter"},
        json={"student_id": "reporter", "seat_id": res["seat_id"], "note": "只有书"},
    )
    assert report.status_code == 200
    assert report.json()["holder_flagged"] == "holder"
    complaints = client.get("/api/v1/admin/complaints?status=open", headers=ADMIN).json()
    assert complaints["complaints"][0]["seat_id"] == res["seat_id"]
    resolved = client.post(
        f"/api/v1/admin/complaints/{complaints['complaints'][0]['id']}/resolve",
        headers=ADMIN,
        json={"note": "已清理"},
    )
    assert resolved.status_code == 200
    violations = client.get("/api/v1/admin/violations", headers=ADMIN).json()["violations"]
    waived = client.post(
        f"/api/v1/admin/violations/{violations[0]['id']}/waive",
        headers=ADMIN,
        json={"note": "申诉通过"},
    )
    assert waived.status_code == 200 and waived.json()["waived"] is True


def test_export_and_import(client: TestClient) -> None:
    exported = client.get("/api/v1/admin/export", headers=ADMIN).json()
    assert set(exported) >= {"venue", "seats", "reservations", "audit"}
    imported = client.post(
        "/api/v1/admin/import",
        headers=ADMIN,
        json={"venue": {**exported["venue"], "name": "导入馆", "cap": 90}},
    )
    assert imported.status_code == 200 and imported.json()["name"] == "导入馆"
    assert len(client.get("/api/v1/admin/seats", headers=ADMIN).json()["seats"]) == 90


def test_announcements_reach_the_student_app(client: TestClient) -> None:
    client.post(
        "/api/v1/admin/announcements",
        headers=ADMIN,
        json={"title": "延时闭馆", "body": "本周五延至 23:00"},
    )
    feed = client.get("/api/v1/app/announcements").json()["announcements"]
    assert feed[0]["title"] == "延时闭馆"
    listing = client.get("/api/v1/admin/announcements", headers=ADMIN).json()["announcements"]
    assert listing[0]["id"] == feed[0]["id"]
    off = client.post(f"/api/v1/admin/announcements/{feed[0]['id']}/deactivate", headers=ADMIN)
    assert off.status_code == 200 and off.json()["active"] is False
    assert client.get("/api/v1/app/announcements").json()["announcements"] == []
    assert (
        client.post(
            f"/api/v1/admin/announcements/{feed[0]['id']}/deactivate", headers=ADMIN
        ).status_code
        == 404
    )


def test_both_pages_are_served(client: TestClient) -> None:
    admin = client.get("/admin")
    assert admin.status_code == 200 and "控制台" in admin.text
    app_page = client.get("/app")
    assert app_page.status_code == 200 and "STUDENT APP" in app_page.text


def test_waitlist_endpoint(client: TestClient) -> None:
    join = client.post(
        "/api/v1/app/waitlist", headers=APP, json={"student_id": "20241001", "zone": "B"}
    )
    assert join.status_code == 200 and join.json()["position"] == 1
    left = client.delete("/api/v1/app/waitlist?student_id=20241001&zone=B", headers=APP)
    assert left.status_code == 200 and left.json()["left"] is True


def test_admin_can_list_and_force_release_active_reservations(client: TestClient) -> None:
    created = client.post(
        "/api/v1/app/reservations", headers=APP, json={"zone": "A", "minutes": 120}
    ).json()
    listing = client.get(
        "/api/v1/admin/reservations?active=true&limit=50", headers=ADMIN
    ).json()["reservations"]
    assert len(listing) == 1
    assert listing[0]["id"] == created["id"]
    assert listing[0]["seat_label"].startswith("A-")
    released = client.post(
        f"/api/v1/admin/reservations/{created['id']}/release", headers=ADMIN
    )
    assert released.status_code == 200
    assert client.get("/api/v1/admin/reservations?active=true", headers=ADMIN).json()[
        "reservations"
    ] == []


def test_admin_close_requires_confirmation_and_releases_everything(client: TestClient) -> None:
    first = client.post(
        "/api/v1/app/reservations", headers=APP, json={"zone": "A", "minutes": 60}
    ).json()
    second = client.post(
        "/api/v1/app/reservations",
        headers={**APP, "X-Student-ID": "20241002"},
        json={"student_id": "20241002", "zone": "B", "minutes": 60},
    ).json()
    refused = client.post("/api/v1/admin/close", headers=ADMIN)
    assert refused.status_code == 400
    closed = client.post("/api/v1/admin/close?confirm=true", headers=ADMIN)
    assert closed.status_code == 200 and closed.json()["closed"] == 2
    dashboard = client.get("/api/v1/admin/dashboard", headers=ADMIN).json()
    assert dashboard["live"]["by_status"]["reserved"] == 0
    assert first["id"] != second["id"]


def test_venue_zone_change_is_rejected_while_active(client: TestClient) -> None:
    created = client.post(
        "/api/v1/app/reservations", headers=APP, json={"zone": "A", "minutes": 60}
    ).json()
    rejected = client.patch(
        "/api/v1/admin/venue",
        headers=ADMIN,
        json={"zones": [{"key": "A", "name": "Only", "share": 1.0}]},
    )
    assert rejected.status_code == 409
    client.post(f"/api/v1/admin/reservations/{created['id']}/release", headers=ADMIN)
    accepted = client.patch(
        "/api/v1/admin/venue",
        headers=ADMIN,
        json={"zones": [{"key": "A", "name": "Only", "share": 1.0}]},
    )
    assert accepted.status_code == 200
