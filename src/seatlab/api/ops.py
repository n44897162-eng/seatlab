"""HTTP surface for the two audiences of a deployed venue.

* ``/api/v1/admin/*`` — control plane: venue configuration, mechanism activation,
  live dashboard, capacity planning, credit/complaints/announcements, audit and
  config export/import. Guarded by ``X-Admin-Key``.
* ``/api/v1/app/*`` — student app: availability, reserve, check-in, away/back,
  release, hoarding report and waitlist. Guarded by an optional shared
  ``X-App-Key`` (kiosk/campus-network) plus the student's own id.

Both are thin adapters: all rules live in :class:`seatlab.service.OpsService`.
"""

from __future__ import annotations

import hmac
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ..auth import AuthService
from ..service import OpsError, OpsService
from .schemas_ops import (
    AnnounceIn,
    AwayIn,
    CheckinIn,
    ImportIn,
    PlanIn,
    PolicyActivateIn,
    ReportIn,
    ReserveIn,
    ResolveIn,
    StudentOnly,
    VenuePatch,
    WaitlistIn,
    WaiveIn,
)

_RATE_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}


def _check_rate(key: str, limit_per_min: int) -> None:
    if limit_per_min <= 0:
        return
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = [t for t in _RATE.get(key, []) if now - t < 60]
        if len(bucket) >= limit_per_min:
            raise HTTPException(status_code=429, detail="too many requests; retry shortly")
        bucket.append(now)
        _RATE[key] = bucket


def _secret_guard(expected: str | None, header: str, label: str):
    def dependency(value: str | None = Header(default=None, alias=header)) -> None:
        if expected and not hmac.compare_digest(value or "", expected):
            raise HTTPException(status_code=401, detail=f"invalid or missing {header}")

    dependency.__name__ = f"require_{label}"
    return dependency


def build_admin_router(
    service: OpsService, admin_key: str | None, auth: AuthService | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
    cfg = auth.config if auth else None

    def guard(
        request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
    ) -> None:
        """Accept the break-glass admin key, or an SSO session with role=admin."""
        if admin_key and hmac.compare_digest(x_admin_key or "", admin_key):
            return
        if auth is not None and cfg is not None and cfg.admin_sso:
            session = auth.session_from_request(request)
            if session is not None and session.role == "admin":
                auth.require_csrf(request, session)
                return
        if not admin_key and (cfg is None or cfg.mode == "header"):
            return  # development / trusted-network mode, documented in docs/OPS.md
        raise HTTPException(
            status_code=401,
            detail="admin login required (X-Admin-Key or an SSO session with role=admin)",
        )

    def body(fn):
        """Wrap OpsError into an HTTP response and audit admin reads lightly."""
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except OpsError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

        return wrapper

    @router.get("/dashboard", dependencies=[Depends(guard)])
    @body
    def dashboard() -> dict[str, Any]:
        return service.dashboard()

    @router.get("/venue", dependencies=[Depends(guard)])
    @body
    def get_venue() -> dict[str, Any]:
        return service.venue()

    @router.patch("/venue", dependencies=[Depends(guard)])
    @body
    def patch_venue(body: VenuePatch) -> dict[str, Any]:
        return service.update_venue(body.model_dump(exclude_none=True))

    @router.get("/policies", dependencies=[Depends(guard)])
    @body
    def policies() -> dict[str, Any]:
        return service.policy_catalog()

    @router.post("/policies/activate", dependencies=[Depends(guard)])
    @body
    def activate(body: PolicyActivateIn) -> dict[str, Any]:
        return service.activate_policy(body.key, body.overrides)

    @router.post("/plan", dependencies=[Depends(guard)])
    @body
    def plan(body: PlanIn) -> dict[str, Any]:
        return service.plan(body.demands, days=body.days, caps=body.caps)

    @router.get("/seats", dependencies=[Depends(guard)])
    @body
    def seats(zone: str | None = None, status: str | None = None) -> dict[str, Any]:
        return {"seats": service.seats(zone=zone, status=status)}

    @router.get("/reservations", dependencies=[Depends(guard)])
    @body
    def reservations(
        status: str | None = None,
        active: bool = False,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        if active:
            return {"reservations": service.active_reservations(limit=limit)}
        return {"reservations": service.store.reservations(status=status, limit=limit)}

    @router.post("/reservations/{reservation_id}/release", dependencies=[Depends(guard)])
    @body
    def force_release(reservation_id: int) -> dict[str, Any]:
        return service.force_release(reservation_id)

    @router.post("/close", dependencies=[Depends(guard)])
    @body
    def close_venue(confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise HTTPException(
                status_code=400, detail="pass ?confirm=true to close the hall and release all seats"
            )
        return service.close_venue()

    @router.get("/violations", dependencies=[Depends(guard)])
    @body
    def violations(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        return {"violations": service.violations(limit=limit)}

    @router.post("/violations/{violation_id}/waive", dependencies=[Depends(guard)])
    @body
    def waive(violation_id: int, body: WaiveIn) -> dict[str, Any]:
        return service.waive(violation_id, body.note)

    @router.get("/complaints", dependencies=[Depends(guard)])
    @body
    def complaints(status: str | None = None) -> dict[str, Any]:
        return {"complaints": service.complaints(status=status)}

    @router.post("/complaints/{complaint_id}/resolve", dependencies=[Depends(guard)])
    @body
    def resolve(complaint_id: int, body: ResolveIn) -> dict[str, Any]:
        return service.resolve_complaint(complaint_id, body.note)

    @router.get("/announcements", dependencies=[Depends(guard)])
    @body
    def announcements() -> dict[str, Any]:
        return {"announcements": service.announcements(active_only=False)}

    @router.post("/announcements", dependencies=[Depends(guard)])
    @body
    def announce(body: AnnounceIn) -> dict[str, Any]:
        return service.announce(body.title, body.body)

    @router.post("/announcements/{announcement_id}/deactivate", dependencies=[Depends(guard)])
    @body
    def deactivate_announcement(announcement_id: int) -> dict[str, Any]:
        return service.deactivate_announcement(announcement_id)

    @router.get("/audit", dependencies=[Depends(guard)])
    @body
    def audit(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        return {"audit": service.audit(limit)}

    @router.get("/export", dependencies=[Depends(guard)])
    @body
    def export() -> dict[str, Any]:
        return service.export()

    @router.post("/import", dependencies=[Depends(guard)])
    @body
    def import_config(body: ImportIn) -> dict[str, Any]:
        return service.import_config(body.model_dump())

    @router.post("/reset-seats", dependencies=[Depends(guard)])
    @body
    def reset_seats(confirm: bool = False) -> dict[str, Any]:
        active = [
            r
            for r in service.store.reservations(limit=10_000)
            if r["status"] in ("reserved", "checked_in", "away")
        ]
        if active:
            raise HTTPException(
                status_code=409, detail="cannot reset seats with active reservations"
            )
        if not confirm:
            raise HTTPException(status_code=400, detail="pass ?confirm=true to reset seat state")
        service.store.reset_seats()
        service._audit("admin", "seats.reset", {})
        return {"reset": True}

    return router


def build_app_router(
    service: OpsService,
    app_key: str | None,
    rate_per_min: int = 60,
    auth: AuthService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/app", tags=["student"])
    guard = _secret_guard(app_key, "X-App-Key", "app_key")
    cfg = auth.config if auth else None

    def student_header(
        request: Request,
        x_student_id: str | None = Header(default=None, alias="X-Student-ID"),
    ) -> str:
        """Resolve the caller: SSO session first, then a trusted header in dev mode."""
        session = auth.session_from_request(request) if auth is not None else None
        if session is not None:
            auth.require_csrf(request, session)
            return session.student_id
        if cfg is None or cfg.trust_header:
            return x_student_id or ""
        raise HTTPException(status_code=401, detail="login required; open /auth/login")

    @router.get("/config")
    def app_config() -> dict[str, Any]:
        return {
            "mode": cfg.mode if cfg else "header",
            "sso": bool(cfg and cfg.is_sso),
            "login_url": "/auth/login" if cfg and cfg.is_sso else None,
            "header_allowed": bool(cfg.trust_header) if cfg else True,
            "app_key_required": bool(app_key),
            "sso_admin": bool(cfg and cfg.is_sso and cfg.admin_sso),
        }

    def body(fn):
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except OpsError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

        return wrapper

    def who(body_id: str, header_id: str) -> str:
        sid = header_id or body_id
        if not sid:
            raise HTTPException(status_code=401, detail="student id required")
        return sid

    @router.get("/venue")
    def venue() -> dict[str, Any]:
        data = service.venue()
        return {
            "name": data["name"],
            "open_window": [data["open_minute"], data["close_minute"]],
            "zones": [
                {"key": z["key"], "name": z["name"], "seats": z.get("share")} for z in data["zones"]
            ],
            "policy": data["policy_key"],
            "zone_counts": data["zone_counts"],
            "max_reservation_minutes": data["max_reservation_minutes"],
            "max_active_reservations": data["max_active_reservations"],
            "reservation_grace_min": data["reservation_grace_min"],
            "away_limit_min": data["away_limit_min"],
        }

    @router.get("/announcements")
    def announcements() -> dict[str, Any]:
        return {"announcements": service.announcements(active_only=True)}

    @router.get("/availability")
    def availability() -> dict[str, Any]:
        return service.availability()

    @router.get("/seats")
    def seats(zone: str | None = None, status: str | None = None) -> dict[str, Any]:
        return {"seats": service.seats(zone=zone, status=status)}

    @router.post("/reservations", dependencies=[Depends(guard)])
    @body
    def reserve(
        body: ReserveIn,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        _check_rate(f"{sid}:reserve", rate_per_min)
        return service.reserve(sid, seat_id=body.seat_id, zone=body.zone, minutes=body.minutes)

    @router.post("/reservations/{res_id}/checkin", dependencies=[Depends(guard)])
    @body
    def checkin(
        res_id: int,
        body: CheckinIn,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        _check_rate(f"{sid}:checkin", rate_per_min)
        return service.checkin(res_id, sid, body.code)

    @router.post("/reservations/{res_id}/away", dependencies=[Depends(guard)])
    @body
    def away(
        res_id: int,
        body: AwayIn,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        return service.away(res_id, sid, body.minutes)

    @router.post("/reservations/{res_id}/back", dependencies=[Depends(guard)])
    @body
    def back(
        res_id: int,
        body: StudentOnly,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        return service.back(res_id, sid)

    @router.delete("/reservations/{res_id}", dependencies=[Depends(guard)])
    @body
    def release(
        res_id: int,
        student_id: str = Query(..., min_length=1),
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(student_id, header_id)
        return service.release(res_id, sid)

    @router.get("/me")
    def me(
        student_id: str | None = Query(default=None, min_length=1, max_length=40),
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        return service.student_state(who(student_id or "", header_id))

    @router.post("/reports", dependencies=[Depends(guard)])
    @body
    def report(
        body: ReportIn,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        _check_rate(f"{sid}:report", max(3, rate_per_min // 4))
        return service.report(sid, body.seat_id, body.note)

    @router.post("/waitlist", dependencies=[Depends(guard)])
    @body
    def join_waitlist(
        body: WaitlistIn,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        sid = who(body.student_id, header_id)
        return service.join_waitlist(sid, body.zone)

    @router.delete("/waitlist")
    def leave_waitlist(
        student_id: str = Query(..., min_length=1),
        zone: str | None = None,
        header_id: str = Depends(student_header),
    ) -> dict[str, Any]:
        service.store.leave_waitlist(who(student_id, header_id), zone)
        return {"left": True}

    return router
