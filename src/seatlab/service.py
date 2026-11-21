"""Business rules for the deployable seat service (control plane + student app).

Everything a venue actually needs lives here:

* seat inventory per zone, rebuilt when the capacity changes;
* reservations with a 4-digit check-in code, a grace window and a maximum
  session length;
* "away" state with a hard limit, after which the seat is reclaimed and a
  violation is recorded;
* credit points that block a student for N days after the threshold;
* hoarding reports that turn into complaints and, when the holder never
  checked in, into a violation for the holder;
* a waitlist per zone, announcements, an audit trail and export/import.

The service is deliberately synchronous and clock-injectable: ``now`` is a
callable returning a timezone-aware datetime, so tests can jump forward 20
minutes instead of sleeping.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .engine import run
from .model import POLICY_DESCRIPTIONS, PRESET_POLICIES, Params, Policy, resolve_policy
from .store import DEFAULT_VENUE, ConflictError, Store
from .study import sweep

POINTS = {"no_show": 1, "away_overrun": 2, "reported_hoarding": 2}


class OpsError(Exception):
    """An expected, user-facing failure with an HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _at_minute(day: datetime, minute: int) -> datetime:
    """Midnight of ``day`` plus ``minute``; supports close_minute=1440."""
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=minute)


def _is_open(day: datetime, venue: dict[str, Any]) -> bool:
    minute = day.hour * 60 + day.minute
    return int(venue["open_minute"]) <= minute < int(venue["close_minute"])


def _closing_time(day: datetime, venue: dict[str, Any]) -> datetime:
    return _at_minute(day, int(venue["close_minute"]))


def _opening_time(day: datetime, venue: dict[str, Any]) -> datetime:
    return _at_minute(day, int(venue["open_minute"]))



def _split_capacity(cap: int, zones: list[dict[str, Any]]) -> list[int]:
    """Largest-remainder split so zone counts always add up to ``cap``."""
    shares = [float(z.get("share", 0)) for z in zones]
    total = sum(shares) or 1.0
    raw = [cap * s / total for s in shares]
    base = [int(math.floor(x)) for x in raw]
    rest = cap - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in range(rest):
        base[order[i % len(order)]] += 1
    return base


class OpsService:
    def __init__(self, store: Store, now: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now().astimezone())
        self.ensure_seats()

    # ------------------------------------------------------------------ clock
    def now(self) -> datetime:
        return self._now().replace(microsecond=0)

    # ------------------------------------------------------------- inventory
    def ensure_seats(self) -> None:
        venue = self.store.get_venue()
        if not self.store.seats():
            self._build_seats(venue)

    def _build_seats(self, venue: dict[str, Any]) -> None:
        counts = _split_capacity(int(venue["cap"]), venue["zones"])
        seats = []
        sid = 1
        for zone, count in zip(venue["zones"], counts, strict=True):
            for n in range(1, count + 1):
                seats.append(
                    {
                        "id": sid,
                        "label": f"{zone['key']}-{n:03d}",
                        "zone": zone["key"],
                    }
                )
                sid += 1
        self.store.replace_seats(seats)

    def venue(self) -> dict[str, Any]:
        venue = self.store.get_venue()
        seats = self.store.seats()
        venue["zone_counts"] = {
            z["key"]: len([s for s in seats if s["zone"] == z["key"]]) for z in venue["zones"]
        }
        return venue

    def update_venue(self, patch: dict[str, Any]) -> dict[str, Any]:
        venue = self.store.get_venue()
        new = copy.deepcopy(venue)
        new.update(patch)
        self._validate_venue(new)
        layout_change = new["cap"] != venue["cap"] or new["zones"] != venue["zones"]
        if layout_change and self.store.active_reservations_all(limit=10_000):
            raise OpsError(
                409,
                "cannot change seat count or zones while reservations are active; close the hall first",
            )
        self.store.set_venue(new)
        if layout_change:
            self.store.reset_seats()
            self._build_seats(new)
        self._audit("admin", "venue.update", patch)
        return self.venue()

    @staticmethod
    def _validate_venue(venue: dict[str, Any]) -> None:
        if not 10 <= int(venue["cap"]) <= 5000:
            raise OpsError(422, "cap must be within [10, 5000]")
        if not 0 <= int(venue["open_minute"]) < int(venue["close_minute"]) <= 1440:
            raise OpsError(422, "open/close minutes are inconsistent")
        keys = [z["key"] for z in venue["zones"]]
        if not keys or len(set(keys)) != len(keys):
            raise OpsError(422, "zones need unique keys")
        for zone in venue["zones"]:
            key = str(zone.get("key", ""))
            name = str(zone.get("name", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", key):
                raise OpsError(422, f"invalid zone key: {key!r} (1-8 letters, digits, _ or -)")
            if not 1 <= len(name.strip()) <= 40:
                raise OpsError(422, f"zone {key!r} needs a name of 1-40 characters")
            try:
                share = float(zone.get("share", 0))
            except (TypeError, ValueError) as exc:
                raise OpsError(422, f"zone {key!r} share must be a number") from exc
            if not 0 < share <= 1:
                raise OpsError(422, f"zone {key!r} share must be within (0, 1]")
        if venue["policy_key"] not in PRESET_POLICIES:
            raise OpsError(422, f"unknown policy: {venue['policy_key']}")
        if not 1 <= int(venue["max_active_reservations"]) <= 5:
            raise OpsError(422, "max_active_reservations must be within [1, 5]")
        if not 30 <= int(venue["max_reservation_minutes"]) <= 720:
            raise OpsError(422, "max_reservation_minutes must be within [30, 720]")
        if not 1 <= int(venue["credit_window_days"]) <= 365:
            raise OpsError(422, "credit_window_days must be within [1, 365]")

    def policy(self) -> Policy:
        venue = self.store.get_venue()
        return resolve_policy(venue["policy_key"], **venue.get("policy_overrides", {}))

    def activate_policy(self, key: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        if key not in PRESET_POLICIES:
            raise OpsError(422, f"unknown policy: {key}")
        policy = resolve_policy(key, **(overrides or {}))
        venue = self.store.get_venue()
        venue["policy_key"] = key
        venue["policy_overrides"] = overrides or {}
        self.store.set_venue(venue)
        self._audit("admin", "policy.activate", {"key": key, "policy": policy.to_dict()})
        return {"key": key, "policy": policy.to_dict()}

    # ------------------------------------------------------------- expiration
    def _expire(self, now: datetime) -> None:
        """Sweep ended windows, then close the hall if it is outside opening hours.

        Every transition is conditional, so two workers sweeping at the same
        moment cannot both charge a student or free the same seat. Event times
        use the window deadline, not the moment the sweep happened to run: a
        server that was offline overnight should still record yesterday's no-show
        on yesterday's date.
        """
        venue = self.store.get_venue()
        for res in self.store.due_reservations(_iso(now)):
            if res["status"] == "reserved":
                due = _parse(res["expires_at"])
                if not self.store.claim_reservation(
                    res["id"], "reserved", "expired", released_at=_iso(due), reason="no_show"
                ):
                    continue  # another worker expired it first
                self._free(res["seat_id"], res["id"])
                if _is_open(due, venue):
                    self._violate(res["student_id"], res["id"], "no_show", due, "check-in window missed")
            elif res["status"] == "away":
                due = _parse(res["away_until"])
                if not self.store.claim_reservation(
                    res["id"], "away", "released", released_at=_iso(due), reason="away_timeout"
                ):
                    continue
                self._free(res["seat_id"], res["id"])
                if _is_open(due, venue):
                    self._violate(
                        res["student_id"], res["id"], "away_overrun", due, "away window exceeded"
                    )
            elif res["status"] == "checked_in":
                # A session has a fixed end. Without this branch a checked-in seat
                # would stay occupied forever if the student simply walked away.
                due = _parse(res["expires_at"])
                if not self.store.claim_reservation(
                    res["id"], "checked_in", "released", released_at=_iso(due), reason="session_end"
                ):
                    continue
                self._free(res["seat_id"], res["id"])
        self._close_out(now, venue)

    def _close_out(
        self, now: datetime, venue: dict[str, Any] | None = None, *, force: bool = False
    ) -> int:
        """End every active reservation when the hall is closed (or forced)."""
        venue = venue or self.store.get_venue()
        if not force and _is_open(now, venue):
            return 0
        active = self.store.active_reservations_all(limit=10_000)
        if not active:
            return 0
        close_at = now if force else (
            _closing_time(now, venue)
            if now.hour * 60 + now.minute >= int(venue["close_minute"])
            else _closing_time(now - timedelta(days=1), venue)
        )
        closed = 0
        for res in active:
            target = "expired" if res["status"] == "reserved" else "released"
            if self.store.claim_reservation(
                res["id"], res["status"], target, released_at=_iso(close_at), reason="venue_closed"
            ):
                self._free(res["seat_id"], res["id"])
                closed += 1
        if closed:
            self._audit("system", "venue.close", {"reservations": closed, "at": _iso(close_at)})
        return closed

    def close_venue(self) -> dict[str, Any]:
        """Administrator action: end all active reservations now."""
        count = self._close_out(self.now(), force=True)
        if count:
            self._audit("admin", "venue.close.manual", {"reservations": count})
        return {"closed": count}

    def _free(self, seat_id: int, reservation_id: int | None = None) -> None:
        """Release a seat, but only if it is still held by the closing reservation."""
        self.store.free_seat(int(seat_id), reservation_id)


    def _violate(
        self, student_id: str, res_id: int | None, kind: str, now: datetime, note: str
    ) -> None:
        self.store.add_violation(
            {
                "student_id": student_id,
                "reservation_id": res_id,
                "kind": kind,
                "points": POINTS[kind],
                "note": note,
                "created_at": _iso(now),
            }
        )

    # --------------------------------------------------------------- students
    def student_state(self, student_id: str) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        venue = self.store.get_venue()
        window_start = now - timedelta(days=int(venue["credit_window_days"]))
        rows = [
            v
            for v in self.store.violations(
                student_id=student_id, since=_iso(window_start), limit=500
            )
            if not v["waived"]
        ]
        points = sum(int(v["points"]) for v in rows)
        threshold = int(venue["credit_threshold"])
        blocked_until = None
        if points >= threshold and rows:
            blocked_until = _iso(
                _parse(max(v["created_at"] for v in rows))
                + timedelta(days=int(venue["credit_block_days"]))
            )
        active = self.store.active_reservations(student_id)
        labels = {int(seat["id"]): str(seat["label"]) for seat in self.store.seats()}
        for row in active:
            row["seat_label"] = labels.get(int(row["seat_id"]), str(row["seat_id"]))
        waiting = [w for w in self.store.waitlist() if w["student_id"] == student_id]
        waiting_info: list[dict[str, Any]] = []
        for entry in waiting:
            queue = self.store.waitlist(zone=entry["zone"])
            waiting_info.append(
                {
                    "id": entry["id"],
                    "zone": entry["zone"],
                    "position": 1 + [w["id"] for w in queue].index(entry["id"]),
                    "created_at": entry["created_at"],
                }
            )
        position = waiting_info[0]["position"] if waiting_info else None
        return {
            "student_id": student_id,
            "points": points,
            "threshold": threshold,
            "blocked": bool(blocked_until and _parse(blocked_until) > now),
            "blocked_until": blocked_until,
            "violations": rows[:10],
            "active": active,
            "waiting": waiting_info,
            "waitlist_position": position,
        }

    def _assert_student_ok(self, student_id: str) -> None:
        state = self.student_state(student_id)
        if state["blocked"]:
            raise OpsError(403, f"student is blocked until {state['blocked_until']}")

    # ----------------------------------------------------------- reservations
    def reserve(
        self,
        student_id: str,
        *,
        seat_id: int | None = None,
        zone: str | None = None,
        minutes: int | None = None,
    ) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        self._assert_student_ok(student_id)
        venue = self.store.get_venue()
        if not _is_open(now, venue):
            opens = _opening_time(now, venue)
            raise OpsError(409, f"venue is closed; it opens at {opens.strftime('%H:%M')}")
        close_at = _closing_time(now, venue)
        remaining = int((close_at - now).total_seconds() // 60)
        if remaining < 30:
            raise OpsError(409, "fewer than 30 minutes remain before closing")
        zones = {z["key"]: z for z in venue["zones"]}
        if zone is not None and zone not in zones:
            raise OpsError(422, f"unknown zone: {zone}")
        limit = int(venue["max_active_reservations"])
        if len(self.store.active_reservations(student_id)) >= limit:
            raise OpsError(409, f"at most {limit} active reservations per student")

        requested = int(minutes or venue["max_reservation_minutes"])
        maximum = int(venue["max_reservation_minutes"])
        if not 30 <= requested <= maximum:
            raise OpsError(422, f"minutes must be within [30, {maximum}]")
        # Never create a session that runs past closing. The client is told what
        # it actually got through ``requested_minutes`` and ``clamped``.
        minutes = min(requested, maximum, remaining)
        clamped = minutes != requested

        if seat_id is not None:
            seats = [s for s in self.store.seats() if s["id"] == int(seat_id)]
            if not seats:
                raise OpsError(404, f"seat {seat_id} does not exist")
            if seats[0]["status"] != "free":
                raise OpsError(409, f"seat {seat_id} is not free")
            chosen = seats[0]
        else:
            free = self.store.free_seat_ids(zone)
            if not free:
                queue = self.store.join_waitlist(
                    student_id, zone or venue["zones"][0]["key"], _iso(now)
                )
                position = 1 + [
                    w["id"] for w in self.store.waitlist(zone=zone or venue["zones"][0]["key"])
                ].index(queue)
                raise OpsError(409, f"no free seat in this zone; waitlist position {position}")
            chosen = next(s for s in self.store.seats() if s["id"] == free[0])

        code = f"{secrets.randbelow(10_000):04d}"
        grace = int(venue["reservation_grace_min"])
        row = {
            "student_id": student_id,
            "seat_id": int(chosen["id"]),
            "zone": chosen["zone"],
            "code": code,
            "status": "reserved",
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(minutes=grace)),
            "minutes": minutes,
        }
        try:
            res_id = self.store.insert_reservation(row)
        except ConflictError as exc:
            raise OpsError(409, "seat was taken by another student; pick another seat") from exc
        if not self.store.claim_seat(int(chosen["id"]), res_id):
            # The seat row no longer points at a free seat (stale UI or a manual reset);
            # undo our reservation so the database stays consistent.
            self.store.claim_reservation(res_id, "reserved", "released", reason="lost_race")
            raise OpsError(409, "seat was taken by another student; pick another seat")
        self.store.leave_waitlist(student_id, chosen["zone"])
        self._audit(student_id, "reservation.create", {"id": res_id, "seat": chosen["id"]})
        result = self.store.reservation(res_id) or {}
        result["seat_label"] = chosen["label"]
        result["checkin_deadline"] = row["expires_at"]
        result["requested_minutes"] = requested
        result["clamped"] = clamped
        result["close_at"] = _iso(close_at)
        return result

    def _own(self, res_id: int, student_id: str) -> dict[str, Any]:
        res = self.store.reservation(res_id)
        if not res:
            raise OpsError(404, f"reservation {res_id} does not exist")
        if not hmac.compare_digest(str(res["student_id"]), str(student_id)):
            raise OpsError(403, "reservation belongs to another student")
        return res

    def checkin(self, res_id: int, student_id: str, code: str) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        res = self._own(res_id, student_id)
        if res["status"] != "reserved":
            raise OpsError(409, f"reservation is {res['status']}")
        if not hmac.compare_digest(str(res["code"]), str(code)):
            raise OpsError(403, "wrong check-in code")
        if _parse(res["expires_at"]) <= now:
            raise OpsError(410, "check-in window has closed")
        close_at = _closing_time(now, self.store.get_venue())
        end = min(now + timedelta(minutes=int(res["minutes"])), close_at)
        minutes = int((end - now).total_seconds() // 60)
        if minutes <= 0:
            raise OpsError(409, "venue is closing; the seat cannot be checked in now")
        if not self.store.claim_reservation(
            res_id,
            "reserved",
            "checked_in",
            checked_in_at=_iso(now),
            expires_at=_iso(end),
            away_until=None,
            minutes=minutes,
        ):
            raise OpsError(409, "reservation was already processed")
        self.store.occupy_seat(int(res["seat_id"]), res_id, "occupied")
        self._audit(student_id, "reservation.checkin", {"id": res_id})
        out = self.store.reservation(res_id) or {}
        out["seat_label"] = self._label(int(res["seat_id"]))
        return out

    def away(self, res_id: int, student_id: str, minutes: int = 15) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        res = self._own(res_id, student_id)
        if res["status"] != "checked_in":
            raise OpsError(409, f"reservation is {res['status']}")
        limit = int(self.store.get_venue()["away_limit_min"])
        if not 1 <= int(minutes) <= limit:
            raise OpsError(422, f"minutes must be within [1, {limit}]")
        close_at = _closing_time(now, self.store.get_venue())
        until = min(now + timedelta(minutes=int(minutes)), close_at)
        if until <= now:
            raise OpsError(409, "venue is closing; the seat cannot go away now")
        if not self.store.claim_reservation(res_id, "checked_in", "away", away_until=_iso(until)):
            raise OpsError(409, "reservation was already processed")
        self.store.occupy_seat(int(res["seat_id"]), res_id, "away")
        self._audit(student_id, "reservation.away", {"id": res_id, "minutes": minutes})
        out = self.store.reservation(res_id) or {}
        out["seat_label"] = self._label(int(res["seat_id"]))
        return out

    def back(self, res_id: int, student_id: str) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        res = self._own(res_id, student_id)
        if res["status"] != "away":
            raise OpsError(409, f"reservation is {res['status']}")
        if res["away_until"] and _parse(res["away_until"]) <= now:
            raise OpsError(410, "away window has closed; seat was reclaimed")
        if not self.store.claim_reservation(res_id, "away", "checked_in", away_until=None):
            raise OpsError(409, "reservation was already processed")
        self.store.occupy_seat(int(res["seat_id"]), res_id, "occupied")
        self._audit(student_id, "reservation.back", {"id": res_id})
        out = self.store.reservation(res_id) or {}
        out["seat_label"] = self._label(int(res["seat_id"]))
        return out

    def release(self, res_id: int, student_id: str, reason: str = "manual") -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        res = self._own(res_id, student_id)
        if res["status"] not in ("reserved", "checked_in", "away"):
            raise OpsError(409, f"reservation is {res['status']}")
        if not self.store.claim_reservation(
            res_id, res["status"], "released", released_at=_iso(now), reason=reason
        ):
            raise OpsError(409, "reservation was already processed")
        self._free(int(res["seat_id"]), res_id)
        self._audit(student_id, "reservation.release", {"id": res_id, "reason": reason})
        return {"id": res_id, "status": "released"}

    # ------------------------------------------------------------------ query
    def _label(self, seat_id: int) -> str:
        for seat in self.store.seats():
            if seat["id"] == seat_id:
                return str(seat["label"])
        return str(seat_id)

    def seats(self, zone: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        self._expire(self.now())
        seats = self.store.seats()
        if zone:
            seats = [s for s in seats if s["zone"] == zone]
        if status:
            seats = [s for s in seats if s["status"] == status]
        return seats

    def availability(self) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        venue = self.store.get_venue()
        open_now = (
            int(venue["open_minute"]) <= now.hour * 60 + now.minute < int(venue["close_minute"])
        )
        minutes_to_close = (
            max(0, int(venue["close_minute"]) - (now.hour * 60 + now.minute)) if open_now else 0
        )
        seats = self.store.seats()
        zones = []
        for zone in venue["zones"]:
            in_zone = [s for s in seats if s["zone"] == zone["key"]]
            free = len([s for s in in_zone if s["status"] == "free"])
            zones.append(
                {
                    "key": zone["key"],
                    "name": zone["name"],
                    "total": len(in_zone),
                    "free": free,
                    "waiting": len(self.store.waitlist(zone=zone["key"])),
                }
            )
        return {
            "open": open_now,
            "at": _iso(now),
            "open_window": [venue["open_minute"], venue["close_minute"]],
            "minutes_to_close": minutes_to_close,
            "zones": zones,
        }

    def join_waitlist(self, student_id: str, zone: str) -> dict[str, Any]:
        """Join a zone queue, using the same block/eligibility checks as reserving."""
        now = self.now()
        self._expire(now)
        venue = self.store.get_venue()
        if zone not in {z["key"] for z in venue["zones"]}:
            raise OpsError(422, f"unknown zone: {zone}")
        self._assert_student_ok(student_id)
        entry = self.store.join_waitlist(student_id, zone, _iso(now))
        queue = self.store.waitlist(zone=zone)
        position = [w["id"] for w in queue].index(entry) + 1
        self._audit(student_id, "waitlist.join", {"zone": zone, "position": position})
        return {"id": entry, "zone": zone, "position": position}

    # ----------------------------------------------------------------- reports
    def report(self, student_id: str, seat_id: int, note: str) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        seat = next((s for s in self.store.seats() if s["id"] == int(seat_id)), None)
        if not seat:
            raise OpsError(404, f"seat {seat_id} does not exist")
        if not note.strip():
            raise OpsError(422, "note must not be empty")
        complaint_id = self.store.add_complaint(
            {
                "student_id": student_id,
                "seat_id": int(seat_id),
                "note": note.strip(),
                "created_at": _iso(now),
            }
        )
        punished = None
        if seat["status"] == "reserved" and seat["reservation_id"]:
            res = self.store.reservation(int(seat["reservation_id"]))
            if res and res["status"] == "reserved":
                if not self.store.claim_reservation(
                    int(res["id"]), "reserved", "expired", released_at=_iso(now), reason="reported"
                ):
                    return {
                        "complaint_id": complaint_id,
                        "seat_id": int(seat_id),
                        "status": "open",
                        "holder_flagged": None,
                    }
                self._free(int(seat_id), int(res["id"]))
                self._violate(
                    res["student_id"],
                    int(res["id"]),
                    "reported_hoarding",
                    now,
                    "reported before check-in",
                )
                punished = res["student_id"]
        self._audit(student_id, "seat.report", {"seat": seat_id, "complaint": complaint_id})
        return {
            "complaint_id": complaint_id,
            "seat_id": int(seat_id),
            "status": "open",
            "holder_flagged": punished,
        }

    # -------------------------------------------------------------------- admin
    def dashboard(self) -> dict[str, Any]:
        now = self.now()
        self._expire(now)
        venue = self.store.get_venue()
        seats = self.store.seats()
        counts: dict[str, int] = {}
        for seat in seats:
            counts[seat["status"]] = counts.get(seat["status"], 0) + 1
        total = len(seats) or 1
        today = now.date().isoformat()
        reservations = self.store.reservations(limit=10_000)
        today_res = [r for r in reservations if r["created_at"][:10] == today]
        checked_in = [r for r in today_res if r["checked_in_at"]]
        no_show = [r for r in today_res if r["status"] == "expired" and not r["checked_in_at"]]

        params = Params(
            days=1,
            cap=int(venue["cap"]),
            mean_present_h=float(venue["mean_present_h"]),
            seed=int(venue["seed"]),
        )
        demand = float(venue["demand_scale"])
        active = run(params, self.policy(), demand, 1)
        base = run(params, PRESET_POLICIES["fcfs"], demand, 1)
        live_occupied = counts.get("occupied", 0) + counts.get("away", 0)
        return {
            "at": _iso(now),
            "venue": {
                k: venue[k] for k in ("name", "cap", "open_minute", "close_minute", "policy_key")
            },
            "live": {
                "total": total,
                "by_status": {
                    k: counts.get(k, 0) for k in ("free", "reserved", "occupied", "away")
                },
                "occupancy_rate": round(live_occupied / total, 4),
                "open": int(venue["open_minute"])
                <= now.hour * 60 + now.minute
                < int(venue["close_minute"]),
            },
            "today": {
                "reservations": len(today_res),
                "checked_in": len(checked_in),
                "no_show": len(no_show),
                "checkin_rate": round(len(checked_in) / max(1, len(today_res)), 4),
                "violations": len(self.store.violations(since=today + "T00:00:00", limit=500)),
                "open_complaints": len(self.store.complaints(status="open", limit=500)),
                "waiting": len(self.store.waitlist()),
            },
            "preview": {
                "policy": active["key"],
                "served_rate": round(active["served_rate"], 4),
                "empty_share": round(active["empty_share"], 4),
                "wait": round(active["wait"], 2),
                "welfare_day": round(active["welfare_day"], 1),
                "baseline_fcfs_welfare_day": round(base["welfare_day"], 1),
                "delta_pct": round((active["welfare_day"] / base["welfare_day"] - 1) * 100, 2),
            },
        }

    def plan(
        self, demands: list[float], days: int = 7, caps: list[int] | None = None
    ) -> dict[str, Any]:
        venue = self.store.get_venue()
        base_cap = int(venue["cap"])
        caps = caps or sorted({max(10, base_cap - 100), base_cap, base_cap + 100})
        params = Params(
            days=days,
            cap=base_cap,
            mean_present_h=float(venue["mean_present_h"]),
            seed=int(venue["seed"]),
        )
        return sweep(params, self.policy(), caps=caps, demands=demands, days=days)

    def complaints(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.store.complaints(status=status)

    def resolve_complaint(self, complaint_id: int, note: str = "") -> dict[str, Any]:
        if not any(c["id"] == complaint_id for c in self.store.complaints(limit=10_000)):
            raise OpsError(404, f"complaint {complaint_id} does not exist")
        self.store.resolve_complaint(complaint_id, _iso(self.now()))
        self._audit("admin", "complaint.resolve", {"id": complaint_id, "note": note})
        return {"id": complaint_id, "status": "resolved"}

    def violations(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.violations(limit=limit)

    def waive(self, violation_id: int, note: str = "") -> dict[str, Any]:
        if not any(v["id"] == violation_id for v in self.store.violations(limit=10_000)):
            raise OpsError(404, f"violation {violation_id} does not exist")
        self.store.waive_violation(violation_id)
        self._audit("admin", "violation.waive", {"id": violation_id, "note": note})
        return {"id": violation_id, "waived": True}

    def announcements(self, active_only: bool = True) -> list[dict[str, Any]]:
        return self.store.announcements(active_only=active_only)

    def announce(self, title: str, body: str) -> dict[str, Any]:
        if not title.strip() or not body.strip():
            raise OpsError(422, "title and body must not be empty")
        ann_id = self.store.add_announcement(title.strip(), body.strip(), _iso(self.now()))
        self._audit("admin", "announcement.create", {"id": ann_id})
        return {"id": ann_id, "title": title, "body": body, "active": True}

    def deactivate_announcement(self, announcement_id: int) -> dict[str, Any]:
        if not self.store.deactivate_announcement(int(announcement_id)):
            raise OpsError(404, "announcement is missing or already inactive")
        self._audit("admin", "announcement.deactivate", {"id": announcement_id})
        return {"id": int(announcement_id), "active": False}

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.audit(limit=limit)

    def export(self) -> dict[str, Any]:
        return self.store.export()

    def import_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        venue = payload.get("venue")
        if not isinstance(venue, dict):
            raise OpsError(422, "payload must contain a 'venue' object")
        new = {**DEFAULT_VENUE, **venue}
        self._validate_venue(new)
        old = self.store.get_venue()
        layout_change = new["cap"] != old["cap"] or new["zones"] != old["zones"]
        if layout_change and self.store.active_reservations_all(limit=10_000):
            raise OpsError(409, "cannot import a new layout while reservations are active")
        self.store.set_venue(new)
        if layout_change:
            self.store.reset_seats()
            self._build_seats(self.store.get_venue())
        self._audit("admin", "config.import", {"keys": sorted(payload.keys())})
        return self.venue()

    def active_reservations(self, limit: int = 200) -> list[dict[str, Any]]:
        """Active reservations with human-readable seat labels for the console."""
        self._expire(self.now())
        labels = {int(s["id"]): str(s["label"]) for s in self.store.seats()}
        rows = self.store.active_reservations_all(limit=limit)
        for row in rows:
            row["seat_label"] = labels.get(int(row["seat_id"]), str(row["seat_id"]))
        return rows

    def force_release(self, reservation_id: int, reason: str = "admin_release") -> dict[str, Any]:
        self._expire(self.now())
        res = self.store.reservation(int(reservation_id))
        if not res:
            raise OpsError(404, f"reservation {reservation_id} does not exist")
        if res["status"] not in ("reserved", "checked_in", "away"):
            raise OpsError(409, f"reservation is {res['status']}")
        now = self.now()
        if not self.store.claim_reservation(
            int(reservation_id), res["status"], "released", released_at=_iso(now), reason=reason
        ):
            raise OpsError(409, "reservation was already processed")
        self._free(int(res["seat_id"]), int(reservation_id))
        self._audit("admin", "reservation.force_release", {"id": int(reservation_id)})
        return {"id": int(reservation_id), "status": "released"}

    # ------------------------------------------------------------------- misc
    def _audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        self.store.log(_iso(self.now()), actor, action, detail)

    def policy_catalog(self) -> dict[str, Any]:
        return {
            "presets": {
                key: {
                    "name": p.name,
                    "description": POLICY_DESCRIPTIONS[key],
                    "params": p.to_dict(),
                }
                for key, p in PRESET_POLICIES.items()
            },
            "active": {
                "key": self.store.get_venue()["policy_key"],
                "overrides": self.store.get_venue().get("policy_overrides", {}),
            },
        }

    def dump(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.export(), ensure_ascii=False, indent=2))
