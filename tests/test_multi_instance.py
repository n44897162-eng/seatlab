"""Multi-worker safety: database constraints instead of a process-local lock.

These tests open **two independent Store objects on the same database**, which
is the closest a unit test can get to two uvicorn workers. They also run the
same contract against PostgreSQL when ``pgserver`` is installed, because the
partial indexes and conditional updates are the part most likely to differ
between backends.
"""

from __future__ import annotations

import pytest

from seatlab.service import OpsError, OpsService
from seatlab.store import ConflictError, Store


def reservation_row(seat_id: int, student_id: str = "20241001") -> dict:
    return {
        "student_id": student_id,
        "seat_id": seat_id,
        "zone": "A",
        "code": "1234",
        "status": "reserved",
        "created_at": "2026-11-21T09:00:00",
        "expires_at": "2026-11-21T09:15:00",
        "minutes": 240,
    }


def test_two_stores_cannot_hold_the_same_seat(tmp_path):
    url = str(tmp_path / "shared.db")
    a, b = Store(url), Store(url)
    assert a.backend == b.backend == "sqlite"
    svc_a = OpsService(a)
    seat = svc_a.store.seats()[0]
    svc_a.reserve("20241001", seat_id=seat["id"])
    with pytest.raises(ConflictError):
        b.insert_reservation(reservation_row(seat["id"], "20241002"))
    with pytest.raises(OpsError) as exc:
        OpsService(b).reserve("20241002", seat_id=seat["id"])
    assert exc.value.status == 409
    assert a.reservation(1) is not None
    a.close()
    b.close()


def test_conditional_transition_has_exactly_one_winner(tmp_path):
    url = str(tmp_path / "shared.db")
    a, b = Store(url), Store(url)
    svc = OpsService(a)
    seat = svc.store.seats()[0]
    res_id = svc.reserve("20241001", seat_id=seat["id"])["id"]
    a.update_reservation(res_id, status="reserved")
    assert a.claim_reservation(
        res_id, "reserved", "expired", released_at="2026-11-21T09:20:00", reason="no_show"
    )
    assert not b.claim_reservation(
        res_id, "reserved", "expired", released_at="2026-11-21T09:21:00", reason="no_show"
    )
    a.free_seat(seat["id"], res_id)
    assert b.free_seat(seat["id"], res_id) is False
    a.close()
    b.close()


def test_duplicate_violation_is_idempotent(tmp_path):
    url = str(tmp_path / "shared.db")
    a, b = Store(url), Store(url)
    row = {
        "student_id": "20241001",
        "reservation_id": 7,
        "kind": "no_show",
        "points": 1,
        "note": "expired",
        "created_at": "2026-11-21T09:20:00",
    }
    first = a.add_violation(row)
    second = b.add_violation(row)
    assert first == second
    a.add_violation({**row, "kind": "no_show"})
    assert len(a.violations(student_id="20241001")) == 1
    a.close()
    b.close()


def test_sessions_are_shared_across_workers(tmp_path):
    url = str(tmp_path / "shared.db")
    a, b = Store(url), Store(url)
    a.create_session(
        {
            "token_hash": "hash-1",
            "student_id": "20241001",
            "role": "student",
            "csrf": "csrf-1",
            "created_at": "2026-11-21T09:00:00",
            "expires_at": "2026-11-21T21:00:00",
        }
    )
    assert b.session("hash-1")["student_id"] == "20241001"
    assert b.revoke_session("hash-1") is True
    assert a.session("hash-1") is None
    a.close()
    b.close()


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    data = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(data / "pg"))
    yield server.get_uri()
    server.cleanup()


@pytest.fixture()
def pg_clean(pg_url):
    """Give every PostgreSQL test a clean slate without restarting the server."""
    import psycopg

    with psycopg.connect(pg_url, autocommit=True) as conn:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        }
        for table in (
            "reservations",
            "violations",
            "complaints",
            "announcements",
            "waitlist",
            "audit",
            "sessions",
        ):
            if table in existing:
                conn.execute(f"DELETE FROM {table}")
    yield


def test_postgres_store_round_trip(pg_url, pg_clean):
    store = Store(pg_url)
    assert store.backend == "postgres"
    assert store.schema_version == 3
    svc = OpsService(store)
    venue = svc.venue()
    assert venue["cap"] == len(store.seats())
    seat = store.seats()[0]
    res = svc.reserve("20241001", seat_id=seat["id"])
    assert store.reservation(res["id"])["status"] == "reserved"
    code = res["code"]
    checked = svc.checkin(res["id"], "20241001", code)
    assert checked["status"] == "checked_in"
    export = store.export()
    assert export["reservations"][0]["id"] == res["id"]
    # A second store sees the same state, i.e. it is really in PostgreSQL.
    other = Store(pg_url)
    assert other.reservation(res["id"])["status"] == "checked_in"
    with pytest.raises(ConflictError):
        other.insert_reservation(reservation_row(seat["id"], "20241002"))
    other.close()
    store.close()


def test_postgres_unique_violation_and_session(pg_url, pg_clean):
    store = Store(pg_url)
    store.reset_seats()
    seat = store.seats()[0]
    store.insert_reservation(reservation_row(seat["id"], "pg-a"))
    with pytest.raises(ConflictError):
        store.insert_reservation(reservation_row(seat["id"], "pg-b"))
    row = {
        "student_id": "pg-a",
        "reservation_id": 999,
        "kind": "away_overrun",
        "points": 2,
        "note": "test",
        "created_at": "2026-11-21T10:00:00",
    }
    assert store.add_violation(row) == store.add_violation(row)
    store.create_session(
        {
            "token_hash": "pg-session",
            "student_id": "pg-a",
            "role": "student",
            "csrf": "csrf",
            "created_at": "2026-11-21T10:00:00",
            "expires_at": "2026-11-21T22:00:00",
        }
    )
    assert store.session("pg-session") is not None
    store.close()


def test_app_runs_on_postgres(pg_url, pg_clean):
    from fastapi.testclient import TestClient

    from seatlab.api.app import create_app

    app = create_app(db_path=pg_url)
    tc = TestClient(app)
    health = tc.get("/healthz").json()
    assert health["storage"] == "postgres"
    assert health["schema_version"] == 3
    assert tc.get("/api/v1/app/availability").status_code == 200
    reserved = tc.post(
        "/api/v1/app/reservations",
        json={"student_id": "20241001", "zone": "A"},
        headers={"X-Student-ID": "20241001"},
    )
    assert reserved.status_code == 200
    app.state.store.close()


def test_expired_sessions_are_not_counted_as_active():
    store = Store(":memory:")
    base = {
        "student_id": "20241001",
        "role": "student",
        "name": "王小明",
        "email": "",
        "csrf": "c",
        "created_at": "2026-11-21T08:00:00",
    }
    store.create_session(
        {**base, "token_hash": "a" * 64, "expires_at": "2026-11-21T08:30:00"}
    )
    store.create_session(
        {**base, "token_hash": "b" * 64, "expires_at": "2026-11-21T10:00:00"}
    )
    rows = store.sessions(
        active_only=True, limit=10, now_iso="2026-11-21T09:00:00"
    )
    assert [row["token_hash"] for row in rows] == ["b" * 64]
