"""FastAPI application factory.

The app is intentionally thin: every endpoint converts a validated request
into the same ``Params`` / ``Policy`` objects the CLI uses, calls the pure
engine, and returns a typed response. There is no global mutable state, so
the service is safe to run with several uvicorn workers.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..auth import AuthConfig, AuthError, AuthService
from ..constants import DAY_MINUTES, HOUR_WEIGHTS
from ..engine import run
from ..model import POLICY_DESCRIPTIONS, PRESET_POLICIES, Params, Policy
from ..service import OpsError, OpsService
from ..store import Store
from ..study import calibrate, optimize, pricing_curve, scale_up, sweep
from .schemas import (
    CalibrateIn,
    CalibrateOut,
    CompareIn,
    CompareOut,
    HealthOut,
    MetricsOut,
    OptimizeIn,
    OptimizeOut,
    ParamsIn,
    PricingIn,
    PricingOut,
    ScaleUpIn,
    ScaleUpOut,
    SimulateIn,
    SimulateOut,
    SweepIn,
    SweepOut,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

OPENAPI_DESCRIPTION = """
**SeatLab** turns academic-library seat hoarding into a discrete-event queueing
model and exposes it as an API.

A seat is a server with fixed capacity. A student is an arrival with a length of
stay, a few planned absences and a hoarding propensity. One simulated day is
840 one-minute slots; the endpoints below average over N days.

Mechanisms are pure functions of the population and the random seed, so two
policies can be compared *pair by pair* on the same students.

* `POST /api/v1/simulate` — one policy, one scenario
* `POST /api/v1/compare` — up to nine policies, same random stream
* `POST /api/v1/optimize` — grid search over release threshold × trigger
* `POST /api/v1/pricing` — welfare / equity / revenue along an hourly price
* `POST /api/v1/scale-up` — extrapolate seat-time waste to a national ledger
* `POST /api/v1/sweep` — capacity × demand grid on one mechanism (JSON or CSV)
* `POST /api/v1/calibrate` — fit the model to what a library already observes
"""


# The per-student lists (study, values) are only needed for aggregation inside
# ``run``; dropping them before caching keeps the cache small.
_CACHE_DROP = ("study", "values")


@lru_cache(maxsize=1024)
def _cached_run(params: Params, policy: Policy, demand_scale: float, days: int) -> dict:
    """Memoise pure runs: identical requests return instantly."""
    result = run(params, policy, demand_scale, days)
    return {k: v for k, v in result.items() if k not in _CACHE_DROP}


def _metrics(payload: dict) -> MetricsOut:
    return MetricsOut(**payload)


_STATS_LOCK = threading.Lock()
_STATS: dict[str, object] = {
    "requests": 0,
    "errors": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "engine_seconds": 0.0,
    "by_path": {},
    "started": time.time(),
}


def _bump(path: str, status: int, hits: int, misses: int, seconds: float) -> None:
    with _STATS_LOCK:
        _STATS["requests"] = int(_STATS["requests"]) + 1
        if status >= 400:
            _STATS["errors"] = int(_STATS["errors"]) + 1
        _STATS["cache_hits"] = int(_STATS["cache_hits"]) + hits
        _STATS["cache_misses"] = int(_STATS["cache_misses"]) + misses
        _STATS["engine_seconds"] = float(_STATS["engine_seconds"]) + seconds
        by_path = _STATS["by_path"]
        assert isinstance(by_path, dict)
        by_path[path] = by_path.get(path, 0) + 1


def create_app(
    api_key: str | None = None,
    *,
    admin_key: str | None = None,
    app_key: str | None = None,
    db_path: str | None = None,
    now=None,
    auth_config: AuthConfig | None = None,
    auth_client=None,
) -> FastAPI:
    app = FastAPI(
        title="SeatLab API",
        version=__version__,
        summary="Discrete-event seat allocation for academic libraries",
        description=OPENAPI_DESCRIPTION,
        license_info={"name": "MIT"},
        contact={"name": "SeatLab contributors"},
    )

    store = Store(db_path or os.getenv("SEATLAB_DB") or ":memory:")
    service = OpsService(store, now=now)
    admin_key = admin_key if admin_key is not None else (os.getenv("SEATLAB_ADMIN_KEY") or None)
    app_key = app_key if app_key is not None else (os.getenv("SEATLAB_APP_KEY") or None)
    auth_config = auth_config or AuthConfig.from_env()
    auth = AuthService(auth_config, store, now=now, client=auth_client)
    app.state.store = store
    app.state.service = service
    app.state.auth = auth
    app.state.auth_config = auth_config

    from .auth_routes import build_auth_router
    from .ops import build_admin_router, build_app_router

    app.include_router(build_auth_router(auth, auth_config, admin_key_enabled=bool(admin_key)))
    app.include_router(build_admin_router(service, admin_key, auth))
    app_router = build_app_router(
        service, app_key, rate_per_min=int(os.getenv("SEATLAB_APP_RATE", "60")), auth=auth
    )
    app.include_router(app_router)

    @app.exception_handler(OpsError)
    async def _ops_error(_request: Request, exc: OpsError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.exception_handler(AuthError)
    async def _auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.add_middleware(GZipMiddleware, minimum_size=512)
    origins = os.getenv("SEATLAB_CORS_ORIGINS", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def instrument(request: Request, call_next):
        before = _cached_run.cache_info()
        started = time.perf_counter()
        response = await call_next(request)
        after = _cached_run.cache_info()
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = (
            request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        )
        response.headers["X-SeatLab-Runtime-Ms"] = f"{elapsed_ms:.1f}"
        if after.hits > before.hits or after.misses > before.misses:
            response.headers["X-SeatLab-Cache"] = "hit" if after.hits > before.hits else "miss"
        if request.url.path != "/metrics":
            _bump(
                request.url.path,
                response.status_code,
                after.hits - before.hits,
                after.misses - before.misses,
                elapsed_ms / 1000.0,
            )
        return response

    def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        if api_key and not hmac.compare_digest(x_api_key or "", api_key):
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.exception_handler(ValueError)
    async def _value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz", response_model=HealthOut, tags=["ops"])
    def healthz() -> HealthOut:
        return HealthOut(
            status="ok",
            version=__version__,
            engine="discrete-event",
            storage=store.backend,
            schema_version=store.schema_version,
            auth_mode=auth_config.mode,
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics_text() -> Response:
        with _STATS_LOCK:
            body = [
                "# HELP seatlab_info Static build information.",
                "# TYPE seatlab_info gauge",
                f'seatlab_info{{version="{__version__}",storage="{store.backend}",auth="{auth_config.mode}"}} 1',
                "# HELP seatlab_requests_total HTTP requests served.",
                "# TYPE seatlab_requests_total counter",
                f"seatlab_requests_total {_STATS['requests']}",
                "# HELP seatlab_errors_total Responses with status >= 400.",
                "# TYPE seatlab_errors_total counter",
                f"seatlab_errors_total {_STATS['errors']}",
                "# HELP seatlab_cache_hits_total Simulation runs served from the LRU cache.",
                "# TYPE seatlab_cache_hits_total counter",
                f"seatlab_cache_hits_total {_STATS['cache_hits']}",
                "# HELP seatlab_cache_misses_total Simulation runs computed from scratch.",
                "# TYPE seatlab_cache_misses_total counter",
                f"seatlab_cache_misses_total {_STATS['cache_misses']}",
                "# HELP seatlab_engine_seconds_total Wall time spent inside request handlers.",
                "# TYPE seatlab_engine_seconds_total counter",
                f"seatlab_engine_seconds_total {_STATS['engine_seconds']:.3f}",
                "# HELP seatlab_uptime_seconds Process uptime.",
                "# TYPE seatlab_uptime_seconds gauge",
                f"seatlab_uptime_seconds {time.time() - float(_STATS['started']):.0f}",
                "# HELP seatlab_requests_by_path Requests per route.",
                "# TYPE seatlab_requests_by_path counter",
            ]
            by_path = _STATS["by_path"]
            assert isinstance(by_path, dict)
            body.extend(
                f'seatlab_requests_by_path{{path="{p}"}} {c}' for p, c in sorted(by_path.items())
            )
        return Response("\n".join(body) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/meta", tags=["meta"])
    def meta() -> dict:
        return {
            "version": __version__,
            "storage": store.backend,
            "auth": {
                "mode": auth_config.mode,
                "trust_header": auth_config.trust_header,
                "admin_sso": auth_config.admin_sso,
            },
            "algorithm": {
                "day_minutes": DAY_MINUTES,
                "open_from": "08:00",
                "minute_step": 1,
                "hour_weight_shape": list(HOUR_WEIGHTS),
                "seat_states": ["free", "present", "held", "booked"],
                "welfare": "study hours × value - queue minutes × waiting cost - monitoring cost",
            },
            "defaults": Params().to_dict(),
            "policies": {
                key: {
                    "name": policy.name,
                    "description": POLICY_DESCRIPTIONS[key],
                    "params": policy.to_dict(),
                }
                for key, policy in PRESET_POLICIES.items()
            },
            "limits": {
                "max_days": 365,
                "max_capacity": 5000,
                "max_compare_policies": 9,
                "max_optimize_cells": 120,
            },
        }

    @app.post(
        "/api/v1/simulate",
        response_model=SimulateOut,
        tags=["simulation"],
        dependencies=[Depends(require_key)],
    )
    def simulate(body: SimulateIn) -> SimulateOut:
        params = body.params.to_params()
        policy = body.policy.to_policy()
        result = _cached_run(params, policy, body.demand_scale, params.days)
        return SimulateOut(
            metrics=_metrics(result),
            welfare_breakdown={
                "study_value_day": round(result["study_value_day"], 2),
                "wait_cost_day": round(result["wait_cost_day"], 2),
                "monitor_day": round(result["monitor_day"], 2),
                "welfare_day": round(result["welfare_day"], 2),
            },
        )

    @app.post(
        "/api/v1/compare",
        response_model=CompareOut,
        tags=["simulation"],
        dependencies=[Depends(require_key)],
    )
    def compare_policies(body: CompareIn) -> CompareOut:
        params = body.params.to_params()
        rows = [
            _cached_run(params, _preset(key), body.demand_scale, params.days)
            for key in body.policies
        ]
        base = rows[0]
        base_welfare = max(base["welfare_day"], 1e-9)
        best = max(rows, key=lambda r: r["welfare_day"])
        return CompareOut(
            rows=[_metrics(r) for r in rows],
            base_key=base["key"],
            best_welfare_key=best["key"],
            welfare_gain_pct={
                r["key"]: round((r["welfare_day"] / base_welfare - 1) * 100, 2) for r in rows
            },
        )

    @app.post(
        "/api/v1/optimize",
        response_model=OptimizeOut,
        tags=["design"],
        dependencies=[Depends(require_key)],
    )
    def optimize_policy(body: OptimizeIn) -> OptimizeOut:
        params = _with_days(body.params, body.days)
        result = optimize(
            params,
            demand_scale=body.demand_scale,
            days=body.days,
            release_values=body.release_values,
            trigger_values=body.trigger_values,
        )
        return OptimizeOut(**result)

    @app.post(
        "/api/v1/pricing",
        response_model=PricingOut,
        tags=["design"],
        dependencies=[Depends(require_key)],
    )
    def pricing(body: PricingIn) -> PricingOut:
        params = _with_days(body.params, body.days)
        return PricingOut(
            **pricing_curve(
                params, demand_scale=body.demand_scale, days=body.days, prices=body.prices
            )
        )

    @app.post(
        "/api/v1/calibrate",
        response_model=CalibrateOut,
        tags=["design"],
        dependencies=[Depends(require_key)],
    )
    def calibrate_model(body: CalibrateIn) -> CalibrateOut:
        result = calibrate(
            body.params.to_params().replace(days=body.days),
            arrivals_per_day=body.targets.arrivals_per_day,
            empty_share=body.targets.empty_share,
            mean_study_h=body.targets.mean_study_h,
            demand_scale=body.demand_scale,
            days=body.days,
            hoard_kappa=body.hoard_kappa,
        )
        return CalibrateOut(**result)

    @app.post(
        "/api/v1/sweep",
        response_model=None,
        tags=["design"],
        dependencies=[Depends(require_key)],
    )
    def sweep_grid(body: SweepIn, format: Literal["json", "csv"] = "json"):
        result = sweep(
            body.params.to_params().replace(days=body.days),
            body.policy,
            caps=body.caps,
            demands=body.demands,
            days=body.days,
        )
        if format == "csv":
            header = "cap,demand,served_rate,empty_share,wait,util,peak_util,welfare_day"
            lines = [header] + [
                ",".join(
                    f"{row[k]:.4f}" if isinstance(row[k], float) else str(row[k])
                    for k in (
                        "cap",
                        "demand",
                        "served_rate",
                        "empty_share",
                        "wait",
                        "util",
                        "peak_util",
                        "welfare_day",
                    )
                )
                for row in result["rows"]
            ]
            return Response(
                "\n".join(lines) + "\n",
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="seatlab-sweep.csv"'},
            )
        return SweepOut(**result)

    @app.post(
        "/api/v1/scale-up",
        response_model=ScaleUpOut,
        tags=["design"],
        dependencies=[Depends(require_key)],
    )
    def scale(body: ScaleUpIn) -> ScaleUpOut:
        return ScaleUpOut(
            **scale_up(
                students_wan=body.students_wan,
                universities=body.universities,
                seats_per_student=body.seats_per_student,
                peak_days=body.peak_days,
                calm_days=body.calm_days,
                open_hours=body.open_hours,
                empty_exam=body.empty_exam,
                empty_calm=body.empty_calm,
                value_per_hour=body.value_per_hour,
            )
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def landing() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/lab", include_in_schema=False)
        def lab() -> FileResponse:
            return FileResponse(STATIC_DIR / "lab.html")

        @app.get("/admin", include_in_schema=False)
        def admin_page() -> FileResponse:
            return FileResponse(STATIC_DIR / "admin.html")

        @app.get("/app", include_in_schema=False)
        def student_page() -> FileResponse:
            return FileResponse(STATIC_DIR / "app.html")

        @app.get("/design", include_in_schema=False)
        def design() -> FileResponse:
            return FileResponse(STATIC_DIR / "design.html")

    return app


def _preset(key: str) -> Policy:
    if key not in PRESET_POLICIES:
        raise ValueError(f"unknown policy {key!r}")
    return PRESET_POLICIES[key]


def _with_days(params_in: ParamsIn, days: int) -> Params:
    return params_in.to_params().replace(days=days)


def get_app() -> FastAPI:
    """Entry point used by ``uvicorn seatlab.api.app:get_app --factory``."""
    return create_app(api_key=os.getenv("SEATLAB_API_KEY") or None)


app = create_app(api_key=os.getenv("SEATLAB_API_KEY") or None)
