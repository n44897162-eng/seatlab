"""Persistence for the deployable seat service — SQLite or PostgreSQL.

The simulator is stateless; a real library deployment is not. This module keeps
the mutable state a venue needs — seats, reservations, credit events,
complaints, announcements, audit trail, sessions and settings — in one place.

Two backends share the same SQL:

* ``sqlite:///path/to/seatlab.db`` (or a bare path, or ``:memory:``) — the
  default, one file, WAL mode, ideal for a single campus server;
* ``postgresql://user:pass@host/db`` — for multi-instance deployments behind a
  load balancer. Requires the optional ``psycopg`` dependency.

Concurrency notes
-----------------
A process-local lock cannot protect invariants when several uvicorn workers or
containers share one database. Two database constraints do the real work:

* a **partial unique index** on ``reservations(seat_id)`` for active statuses,
  so two processes can never hold the same seat at once;
* a **partial unique index** on ``violations(reservation_id, kind)``, so a
  duplicated expiry sweep cannot charge a student twice.

State transitions use conditional ``UPDATE ... WHERE status = <expected>``
instead of read-then-write, and seat releases only clear the seat when it still
points at the reservation being closed. Every method commits on its own, so a
caller never has to remember transaction boundaries.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seats (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    zone       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'free',
    reservation_id INTEGER
);
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL,
    seat_id     INTEGER NOT NULL,
    zone        TEXT NOT NULL,
    code        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    checked_in_at TEXT,
    away_until  TEXT,
    released_at TEXT,
    minutes     INTEGER NOT NULL,
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS violations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL,
    reservation_id INTEGER,
    kind        TEXT NOT NULL,
    points      INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    waived      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS complaints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL,
    seat_id     INTEGER,
    note        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL,
    zone        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    student_id  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'student',
    name        TEXT,
    email       TEXT,
    csrf        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen_at TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_res_student ON reservations(student_id, status);
CREATE INDEX IF NOT EXISTS idx_res_seat ON reservations(seat_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_res_active_seat
    ON reservations(seat_id) WHERE status IN ('reserved','checked_in','away');
CREATE INDEX IF NOT EXISTS idx_viol_student ON violations(student_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_viol_once
    ON violations(reservation_id, kind) WHERE reservation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_wait_zone ON waitlist(zone, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wait_once
    ON waitlist(student_id, zone) WHERE status='waiting';
CREATE INDEX IF NOT EXISTS idx_session_expiry ON sessions(expires_at);
"""

DEFAULT_VENUE: dict[str, Any] = {
    "name": "示例高校图书馆",
    "cap": 500,
    "open_minute": 480,
    "close_minute": 1320,
    "zones": [
        {"key": "A", "name": "静音自习区", "share": 0.55},
        {"key": "B", "name": "协作讨论区", "share": 0.30},
        {"key": "C", "name": "考研专区", "share": 0.15},
    ],
    "policy_key": "smart",
    "policy_overrides": {"release": 30, "trigger": 0.9},
    "reservation_grace_min": 15,
    "away_limit_min": 30,
    "max_active_reservations": 2,
    "max_reservation_minutes": 240,
    "credit_threshold": 3,
    "credit_block_days": 7,
    "credit_window_days": 30,
    "demand_scale": 1.4,
    "mean_present_h": 4.9,
    "seed": 20261121,
}

SCHEMA_VERSION = 3
_POSTGRES_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")


class ConflictError(RuntimeError):
    """A concurrent writer won the race for the same seat or waitlist slot."""


class Store:
    """Thread-safe state store backed by SQLite or PostgreSQL.

    ``url`` may be a filesystem path (``/data/seatlab.db``), ``:memory:``, or a
    PostgreSQL DSN. ``Store.backend`` exposes which one is active.
    """

    def __init__(self, url: str | Path = ":memory:") -> None:
        self.url = str(url).replace("postgresql+psycopg://", "postgresql://", 1)
        if self.url.startswith("sqlite:///"):
            self.url = self.url[len("sqlite:///") :]
            if self.url in ("", ":memory:"):
                self.url = ":memory:"
        self.path = self.url  # backwards-compatible name
        self._lock = threading.RLock()
        if self.url.lower().startswith(_POSTGRES_PREFIXES):
            self.backend = "postgres"
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise RuntimeError(
                    "PostgreSQL backend needs psycopg: pip install 'seatlab[postgres]'"
                ) from exc
            self._db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
            self._init_schema(
                SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            )
        else:
            self.backend = "sqlite"
            if self.url != ":memory:":
                Path(self.url).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.url, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            with self._lock:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA foreign_keys=ON")
            self._init_schema(SCHEMA)
        self._ensure_venue()

    # ------------------------------------------------------------- primitives
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    def _init_schema(self, schema: str) -> None:
        with self._lock:
            if self.backend == "sqlite":
                self._db.executescript(schema)
                self._db.commit()
            else:
                self._db.execute(schema)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Run one statement; return the first column of a RETURNING row."""
        with self._lock:
            cur = self._db.execute(self._sql(sql), params)
            row = cur.fetchone() if cur.description else None
            cur.close()
            if not row:
                return None
            if self.backend == "sqlite":
                return row[0]
            return next(iter(row.values()))

    def _rowcount(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self._db.execute(self._sql(sql), params)
            count = int(cur.rowcount or 0)
            cur.close()
            return count

    def _executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._lock:
            if self.backend == "sqlite":
                cur = self._db.executemany(self._sql(sql), rows)
                cur.close()
            else:
                with self._db.cursor() as cur:
                    cur.executemany(self._sql(sql), rows)
            self._commit()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(self._sql(sql), params)
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        return rows

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            cur = self._db.execute(self._sql(sql), params)
            row = cur.fetchone()
            cur.close()
        return dict(row) if row else None

    def _commit(self) -> None:
        if self.backend == "sqlite":
            self._db.commit()

    # ------------------------------------------------------------------ utils
    def _ensure_venue(self) -> None:
        row = self._fetchone("SELECT value FROM settings WHERE key='venue'")
        if row is None:
            self._execute(
                "INSERT INTO settings(key, value) VALUES('venue', ?)",
                (json.dumps(DEFAULT_VENUE, ensure_ascii=False),),
            )
            self._commit()
        version = self._fetchone("SELECT value FROM settings WHERE key='schema_version'")
        if version is None:
            self._execute(
                "INSERT INTO settings(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._commit()
        elif int(version["value"]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{version['value']} is newer than this build "
                f"(v{SCHEMA_VERSION}); upgrade SeatLab instead of downgrading the database"
            )

    @property
    def schema_version(self) -> int:
        row = self._fetchone("SELECT value FROM settings WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --------------------------------------------------------------- settings
    def get_venue(self) -> dict[str, Any]:
        row = self._fetchone("SELECT value FROM settings WHERE key='venue'")
        return json.loads(row["value"]) if row else dict(DEFAULT_VENUE)

    def set_venue(self, venue: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO settings(key, value) VALUES('venue', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(venue, ensure_ascii=False),),
        )
        self._commit()

    # ------------------------------------------------------------------ seats
    def seats(self) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT id, label, zone, status, reservation_id FROM seats ORDER BY id"
        )

    def replace_seats(self, seats: list[dict[str, Any]]) -> None:
        with self._lock:
            self._execute("DELETE FROM seats")
            self._executemany(
                "INSERT INTO seats(id, label, zone, status, reservation_id) VALUES(?,?,?,?,?)",
                [(s["id"], s["label"], s["zone"], s.get("status", "free"), None) for s in seats],
            )
            self._commit()

    def set_seat_status(self, seat_id: int, status: str, reservation_id: int | None) -> None:
        self._execute(
            "UPDATE seats SET status=?, reservation_id=? WHERE id=?",
            (status, reservation_id, seat_id),
        )
        self._commit()

    def claim_seat(self, seat_id: int, reservation_id: int) -> bool:
        """Mark a free seat reserved for this reservation, only if still free."""
        changed = self._rowcount(
            "UPDATE seats SET status='reserved', reservation_id=? WHERE id=? AND status='free'",
            (reservation_id, seat_id),
        )
        self._commit()
        return changed == 1

    def occupy_seat(self, seat_id: int, reservation_id: int, status: str) -> bool:
        """Set an occupied/away seat, but only while it still belongs to this reservation."""
        changed = self._rowcount(
            "UPDATE seats SET status=? WHERE id=? AND reservation_id=?",
            (status, seat_id, reservation_id),
        )
        self._commit()
        return changed == 1

    def free_seat(self, seat_id: int, reservation_id: int | None = None) -> bool:
        """Release a seat; with ``reservation_id`` only if it is still the holder's."""
        if reservation_id is None:
            changed = self._rowcount(
                "UPDATE seats SET status='free', reservation_id=NULL WHERE id=?", (seat_id,)
            )
        else:
            changed = self._rowcount(
                "UPDATE seats SET status='free', reservation_id=NULL "
                "WHERE id=? AND reservation_id=?",
                (seat_id, reservation_id),
            )
        self._commit()
        return changed == 1

    # ----------------------------------------------------------- reservations
    def insert_reservation(self, row: dict[str, Any]) -> int:
        try:
            res_id = self._execute(
                "INSERT INTO reservations(student_id, seat_id, zone, code, status, created_at,"
                " expires_at, checked_in_at, away_until, released_at, minutes, reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (
                    row["student_id"],
                    row["seat_id"],
                    row["zone"],
                    row["code"],
                    row["status"],
                    row["created_at"],
                    row["expires_at"],
                    row.get("checked_in_at"),
                    row.get("away_until"),
                    row.get("released_at"),
                    row["minutes"],
                    row.get("reason"),
                ),
            )
        except Exception as exc:  # unique active-seat index
            if exc.__class__.__name__ in ("IntegrityError", "UniqueViolation"):
                raise ConflictError(
                    f"seat {row['seat_id']} already has an active reservation"
                ) from exc
            raise
        assert res_id is not None
        self._commit()
        return int(res_id)

    _RES_FIELDS = {
        "status",
        "expires_at",
        "checked_in_at",
        "away_until",
        "released_at",
        "reason",
        "minutes",
    }

    def update_reservation(self, res_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._RES_FIELDS
        if unknown:
            raise ValueError(f"unknown reservation fields: {sorted(unknown)}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        self._execute(
            f"UPDATE reservations SET {assignments} WHERE id=?",
            (*fields.values(), res_id),
        )
        self._commit()

    def claim_reservation(self, res_id: int, expected: str, status: str, **fields: Any) -> bool:
        """Conditionally move a reservation out of ``expected`` status.

        Returns ``False`` when another worker already handled the transition.
        This is the multi-instance replacement for read-then-write.
        """
        unknown = set(fields) - self._RES_FIELDS
        if unknown:
            raise ValueError(f"unknown reservation fields: {sorted(unknown)}")
        assignments = "status=?" + "".join(f", {k}=?" for k in fields)
        changed = self._rowcount(
            f"UPDATE reservations SET {assignments} WHERE id=? AND status=?",
            (status, *fields.values(), res_id, expected),
        )
        self._commit()
        return changed == 1

    def reservation(self, res_id: int) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM reservations WHERE id=?", (res_id,))

    def reservations(
        self,
        *,
        student_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if student_id:
            clauses.append("student_id=?")
            params.append(student_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(
            f"SELECT * FROM reservations {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )

    def active_reservations(self, student_id: str) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM reservations WHERE student_id=? AND status IN"
            " ('reserved','checked_in','away') ORDER BY id",
            (student_id,),
        )

    def active_reservations_all(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Every seat currently held, newest first — used by the control plane."""
        return self._fetchall(
            "SELECT * FROM reservations WHERE status IN"
            " ('reserved','checked_in','away') ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def due_reservations(self, now_iso: str) -> list[dict[str, Any]]:
        """Reservations whose check-in, session or away window has elapsed."""
        return self._fetchall(
            "SELECT * FROM reservations WHERE"
            " (status='reserved' AND expires_at <= ?) OR"
            " (status='checked_in' AND expires_at <= ?) OR"
            " (status='away' AND away_until <= ?)"
            " ORDER BY id",
            (now_iso, now_iso, now_iso),
        )

    def free_seat_ids(self, zone: str | None = None) -> list[int]:
        sql = "SELECT id FROM seats WHERE status='free'"
        params: tuple[Any, ...] = ()
        if zone:
            sql += " AND zone=?"
            params = (zone,)
        return [int(r["id"]) for r in self._fetchall(sql + " ORDER BY id", params)]

    def reset_seats(self) -> None:
        self._execute("UPDATE seats SET status='free', reservation_id=NULL")
        self._commit()

    # -------------------------------------------------------------- violations
    def add_violation(self, row: dict[str, Any]) -> int:
        """Insert one violation; a repeated sweep for the same reservation is a no-op."""
        existing = None
        if row.get("reservation_id") is not None:
            existing = self._fetchone(
                "SELECT id FROM violations WHERE reservation_id=? AND kind=?",
                (row.get("reservation_id"), row["kind"]),
            )
        if existing:
            return int(existing["id"])
        new_id = self._execute(
            "INSERT INTO violations(student_id, reservation_id, kind, points, note,"
            " created_at, waived) VALUES(?,?,?,?,?,?,0) ON CONFLICT DO NOTHING RETURNING id",
            (
                row["student_id"],
                row.get("reservation_id"),
                row["kind"],
                row["points"],
                row.get("note"),
                row["created_at"],
            ),
        )
        self._commit()
        if new_id is not None:
            return int(new_id)
        # Lost the race: the other writer inserted the same (reservation, kind).
        found = self._fetchone(
            "SELECT id FROM violations WHERE reservation_id=? AND kind=?",
            (row.get("reservation_id"), row["kind"]),
        )
        return int(found["id"]) if found else 0

    def violations(
        self, *, student_id: str | None = None, since: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if student_id:
            clauses.append("student_id=?")
            params.append(student_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(
            f"SELECT * FROM violations {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )

    def waive_violation(self, violation_id: int) -> None:
        self._execute("UPDATE violations SET waived=1 WHERE id=?", (violation_id,))
        self._commit()

    # -------------------------------------------------------------- complaints
    def add_complaint(self, row: dict[str, Any]) -> int:
        new_id = self._execute(
            "INSERT INTO complaints(student_id, seat_id, note, status, created_at)"
            " VALUES(?,?,?, 'open', ?) RETURNING id",
            (row["student_id"], row["seat_id"], row["note"], row["created_at"]),
        )
        self._commit()
        return int(new_id or 0)

    def complaints(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM complaints"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        return self._fetchall(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))

    def resolve_complaint(self, complaint_id: int, at: str) -> None:
        self._execute(
            "UPDATE complaints SET status='resolved', resolved_at=? WHERE id=?",
            (at, complaint_id),
        )
        self._commit()

    # ----------------------------------------------------------- announcements
    def announcements(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM announcements"
        if active_only:
            sql += " WHERE active=1"
        return self._fetchall(sql + " ORDER BY id DESC")

    def deactivate_announcement(self, announcement_id: int) -> bool:
        changed = self._rowcount(
            "UPDATE announcements SET active=0 WHERE id=? AND active=1", (announcement_id,)
        )
        self._commit()
        return changed == 1

    def add_announcement(self, title: str, body: str, at: str) -> int:
        new_id = self._execute(
            "INSERT INTO announcements(title, body, active, created_at)"
            " VALUES(?,?,1,?) RETURNING id",
            (title, body, at),
        )
        self._commit()
        return int(new_id or 0)

    # ---------------------------------------------------------------- waitlist
    def join_waitlist(self, student_id: str, zone: str, at: str) -> int:
        existing = self._fetchone(
            "SELECT id FROM waitlist WHERE student_id=? AND zone=? AND status='waiting'",
            (student_id, zone),
        )
        if existing:
            return int(existing["id"])
        new_id = self._execute(
            "INSERT INTO waitlist(student_id, zone, status, created_at)"
            " VALUES(?,?, 'waiting', ?) ON CONFLICT DO NOTHING RETURNING id",
            (student_id, zone, at),
        )
        self._commit()
        if new_id is not None:
            return int(new_id)
        found = self._fetchone(
            "SELECT id FROM waitlist WHERE student_id=? AND zone=? AND status='waiting'",
            (student_id, zone),
        )
        return int(found["id"]) if found else 0

    def leave_waitlist(self, student_id: str, zone: str | None = None) -> None:
        sql = "UPDATE waitlist SET status='cancelled' WHERE student_id=? AND status='waiting'"
        params: tuple[Any, ...] = (student_id,)
        if zone:
            sql += " AND zone=?"
            params = (student_id, zone)
        self._execute(sql, params)
        self._commit()

    def waitlist(self, zone: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM waitlist WHERE status='waiting'"
        params: tuple[Any, ...] = ()
        if zone:
            sql += " AND zone=?"
            params = (zone,)
        return self._fetchall(sql + " ORDER BY id", params)

    # ------------------------------------------------------------------- audit
    def log(self, at: str, actor: str, action: str, detail: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO audit(at, actor, action, detail) VALUES(?,?,?,?)",
            (at, actor, action, json.dumps(detail, ensure_ascii=False, default=str)),
        )
        self._commit()

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        for item in rows:
            item["detail"] = json.loads(item["detail"])
        return rows

    # ---------------------------------------------------------------- sessions
    def create_session(self, row: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO sessions(token_hash, student_id, role, name, email, csrf,"
            " created_at, expires_at, last_seen_at, revoked)"
            " VALUES(?,?,?,?,?,?,?,?,?,0)",
            (
                row["token_hash"],
                row["student_id"],
                row.get("role", "student"),
                row.get("name"),
                row.get("email"),
                row["csrf"],
                row["created_at"],
                row["expires_at"],
                row["created_at"],
            ),
        )
        self._commit()

    def session(self, token_hash: str) -> dict[str, Any] | None:
        return self._fetchone(
            "SELECT * FROM sessions WHERE token_hash=? AND revoked=0", (token_hash,)
        )

    def touch_session(self, token_hash: str, at: str) -> None:
        self._execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (at, token_hash))
        self._commit()

    def revoke_session(self, token_hash: str) -> bool:
        changed = self._rowcount(
            "UPDATE sessions SET revoked=1 WHERE token_hash=? AND revoked=0", (token_hash,)
        )
        self._commit()
        return changed == 1

    def revoke_student_sessions(self, student_id: str) -> int:
        changed = self._rowcount(
            "UPDATE sessions SET revoked=1 WHERE student_id=? AND revoked=0", (student_id,)
        )
        self._commit()
        return changed

    def sessions(
        self, *, active_only: bool = True, limit: int = 200, now_iso: str | None = None
    ) -> list[dict[str, Any]]:
        where = "WHERE revoked=0" if active_only else ""
        params: list[Any] = []
        if active_only and now_iso:
            where += " AND expires_at >= ?"
            params.append(now_iso)
        params.append(limit)
        return self._fetchall(
            f"SELECT * FROM sessions {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
        )

    def purge_sessions(self, now_iso: str) -> int:
        changed = self._rowcount(
            "DELETE FROM sessions WHERE expires_at < ? OR (revoked=1 AND created_at < ?)",
            (now_iso, now_iso),
        )
        self._commit()
        return changed

    # ------------------------------------------------------------------ export
    def export(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "venue": self.get_venue(),
            "seats": self.seats(),
            "reservations": self.reservations(limit=10_000),
            "violations": self.violations(limit=10_000),
            "complaints": self.complaints(limit=10_000),
            "announcements": self.announcements(active_only=False),
            "waitlist": self.waitlist(),
            "audit": self.audit(limit=10_000),
        }
