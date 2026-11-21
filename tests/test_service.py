"""Business-rule tests for the deployable service (no HTTP layer)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from seatlab.service import OpsError, OpsService
from seatlab.store import Store


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, minutes: int) -> None:
        self.at += timedelta(minutes=minutes)


@pytest.fixture()
def clock() -> Clock:
    return Clock(datetime(2026, 11, 21, 8, 30))


@pytest.fixture()
def service(clock: Clock) -> OpsService:
    return OpsService(Store(":memory:"), now=clock)


def test_reserve_checkin_away_back_release(service: OpsService) -> None:
    res = service.reserve("S1", zone="A", minutes=120)
    assert res["status"] == "reserved" and res["seat_label"].startswith("A-")
    with pytest.raises(OpsError) as err:
        service.checkin(res["id"], "S1", "0000" if res["code"] != "0000" else "1111")
    assert err.value.status == 403

    checked = service.checkin(res["id"], "S1", res["code"])
    assert checked["status"] == "checked_in"
    assert service.seats(status="occupied")[0]["id"] == res["seat_id"]

    away = service.away(res["id"], "S1", 15)
    assert away["status"] == "away"
    back = service.back(res["id"], "S1")
    assert back["status"] == "checked_in"

    released = service.release(res["id"], "S1")
    assert released["status"] == "released"
    assert all(s["status"] == "free" for s in service.seats(zone="A"))


def test_not_checking_in_reclaims_the_seat_and_scores(service: OpsService, clock: Clock) -> None:
    res = service.reserve("S2", zone="A")
    clock.advance(16)
    service.seats()  # any read sweeps expired reservations
    assert service.store.reservation(res["id"])["status"] == "expired"
    assert service.seats(status="reserved") == []
    state = service.student_state("S2")
    assert state["points"] == 1 and not state["blocked"]


def test_three_no_shows_block_new_reservations(service: OpsService, clock: Clock) -> None:
    for _ in range(3):
        service.reserve("S3", zone="A")
        clock.advance(16)
        service.seats()
    assert service.student_state("S3")["blocked"] is True
    with pytest.raises(OpsError) as err:
        service.reserve("S3", zone="A")
    assert err.value.status == 403


def test_away_overrun_reclaims_and_scores_two(service: OpsService, clock: Clock) -> None:
    res = service.reserve("S4", zone="B")
    service.checkin(res["id"], "S4", res["code"])
    service.away(res["id"], "S4", 15)
    clock.advance(16)
    service.availability()
    assert service.store.reservation(res["id"])["status"] == "released"
    assert service.student_state("S4")["points"] == 2


def test_at_most_two_active_reservations(service: OpsService) -> None:
    service.reserve("S5", zone="A")
    service.reserve("S5", zone="B")
    with pytest.raises(OpsError) as err:
        service.reserve("S5", zone="C")
    assert err.value.status == 409


def test_capacity_change_is_blocked_while_seats_are_in_use(service: OpsService) -> None:
    service.reserve("S6", zone="A")
    with pytest.raises(OpsError) as err:
        service.update_venue({"cap": 400})
    assert err.value.status == 409
    service.release(service.store.active_reservations("S6")[0]["id"], "S6")
    assert service.update_venue({"cap": 400})["cap"] == 400


def test_report_flags_a_holder_who_never_checked_in(service: OpsService) -> None:
    res = service.reserve("S7", zone="C")
    out = service.report("S8", int(res["seat_id"]), "只有书，没人")
    assert out["status"] == "open"
    assert out["holder_flagged"] == "S7"
    assert service.store.reservation(res["id"])["status"] == "expired"
    assert service.student_state("S7")["points"] == 2
    assert service.complaints(status="open")


def test_waitlist_position_when_a_zone_is_full(service: OpsService) -> None:
    single = [{"key": "A", "name": "Only", "share": 1.0}]
    service.update_venue({"cap": 10, "zones": single})
    holders = []
    for i in range(10):
        holders.append(service.reserve(f"W{i}", zone="A"))
    with pytest.raises(OpsError) as err:
        service.reserve("W-late", zone="A")
    assert "waitlist position 1" in err.value.detail
    assert service.store.waitlist(zone="A")[0]["student_id"] == "W-late"
    service.release(holders[0]["id"], "W0")
    again = service.reserve("W-late", zone="A")
    assert again["status"] == "reserved"
    assert service.store.waitlist(zone="A") == []


def test_export_import_roundtrip(service: OpsService) -> None:
    payload = service.export()
    assert payload["venue"]["cap"] == 500
    service.import_config({"venue": {**payload["venue"], "name": "新馆", "cap": 120}})
    assert service.venue()["name"] == "新馆"
    assert len(service.seats()) == 120


def test_state_survives_reopen(tmp_path) -> None:
    path = tmp_path / "seatlab.db"
    first = OpsService(Store(path), now=lambda: datetime(2026, 11, 21, 9, 0))
    res = first.reserve("P1", zone="A")
    reopened = OpsService(Store(path), now=lambda: datetime(2026, 11, 21, 9, 1))
    assert reopened.store.reservation(res["id"])["status"] == "reserved"
    assert reopened.seats(status="reserved")[0]["id"] == res["seat_id"]


def test_dashboard_and_plan_are_consistent(service: OpsService) -> None:
    service.reserve("D1", zone="A")
    dash = service.dashboard()
    assert dash["today"]["reservations"] == 1
    assert dash["live"]["total"] == 500
    assert dash["preview"]["policy"] == "smart"
    plan = service.plan([1.0, 1.4], days=3)
    assert len(plan["rows"]) == 6
    assert plan["best"]["welfare_day"] == max(r["welfare_day"] for r in plan["rows"])


def test_reservation_is_clamped_before_closing(service: OpsService, clock: Clock) -> None:
    clock.at = clock.at.replace(hour=21, minute=30)
    res = service.reserve("C1", zone="A", minutes=240)
    assert res["minutes"] == 30
    assert res["clamped"] is True
    assert res["checkin_deadline"][11:16] == "21:45"
    clock.advance(5)
    checked = service.checkin(res["id"], "C1", res["code"])
    assert checked["minutes"] == 25
    assert checked["expires_at"][11:16] == "22:00"


def test_reservation_is_rejected_when_closed(service: OpsService, clock: Clock) -> None:
    clock.at = clock.at.replace(hour=22, minute=30)
    with pytest.raises(OpsError) as err:
        service.reserve("C2", zone="A")
    assert err.value.status == 409
    assert "closed" in err.value.detail


def test_checked_in_session_end_releases_the_seat(service: OpsService, clock: Clock) -> None:
    res = service.reserve("C3", zone="A", minutes=60)
    service.checkin(res["id"], "C3", res["code"])
    clock.advance(61)
    service.seats()
    stored = service.store.reservation(res["id"])
    assert stored["status"] == "released"
    assert stored["reason"] == "session_end"
    assert service.seats(zone="A")[0]["status"] == "free"


def test_zone_change_is_blocked_while_reservations_are_active(
    service: OpsService,
) -> None:
    service.reserve("C4", zone="A")
    with pytest.raises(OpsError) as err:
        service.update_venue({"zones": [{"key": "A", "name": "Only", "share": 1.0}]})
    assert err.value.status == 409
    service.release(service.store.active_reservations("C4")[0]["id"], "C4")
    assert service.update_venue(
        {"zones": [{"key": "A", "name": "Only", "share": 1.0}]}
    )["zone_counts"]["A"] == 500


def test_import_layout_is_blocked_while_reservations_are_active(service: OpsService) -> None:
    service.reserve("C5", zone="A")
    with pytest.raises(OpsError) as err:
        service.import_config(
            {"venue": {"name": "新馆", "cap": 120, "zones": [{"key": "A", "name": "Only", "share": 1.0}]}}
        )
    assert err.value.status == 409


def test_expiry_uses_the_deadline_not_the_sweep_time(service: OpsService, clock: Clock) -> None:
    res = service.reserve("C6", zone="A")
    clock.advance(20)
    service.seats()
    violation = service.store.violations(student_id="C6")[0]
    assert violation["created_at"] == res["expires_at"]


def test_force_release_and_close_venue(service: OpsService, clock: Clock) -> None:
    first = service.reserve("C7", zone="A")
    second = service.reserve("C8", zone="B")
    assert service.force_release(first["id"])["status"] == "released"
    assert service.store.reservation(second["id"])["status"] == "reserved"
    out = service.close_venue()
    assert out["closed"] == 1
    assert service.store.reservation(second["id"])["status"] == "expired"
    assert all(seat["status"] == "free" for seat in service.seats())
    assert len(service.active_reservations()) == 0


def test_waitlist_details_are_returned_for_each_zone(service: OpsService, clock: Clock) -> None:
    single = [{"key": "A", "name": "Only", "share": 1.0}]
    service.update_venue({"cap": 10, "zones": single})
    for i in range(10):
        service.reserve(f"Q{i}", zone="A")
    out = service.join_waitlist("Q-late", "A")
    assert out["position"] == 1
    state = service.student_state("Q-late")
    assert state["waiting"][0]["zone"] == "A"
    assert state["waitlist_position"] == 1
    service.store.leave_waitlist("Q-late", "A")
    assert service.student_state("Q-late")["waiting"] == []
