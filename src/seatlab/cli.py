"""Command line interface.

    seatlab simulate  --policy smart --demand 1.4 --days 40
    seatlab compare   --preset exam --days 16
    seatlab optimize  --days 16
    seatlab pricing   --days 16
    seatlab validate
    seatlab study     --out results.json
    seatlab serve     --host 0.0.0.0 --port 8000

Every command prints JSON by default so it composes with ``jq``; ``--table``
gives a compact human view for the two most-used commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

from . import __version__
from .engine import run
from .model import PRESET_POLICIES, Params, presets, resolve_policy
from .study import (
    calibrate,
    compare,
    optimize,
    pricing_curve,
    run_study,
    scale_up,
    sweep,
    validate_queueing,
)


def _params(args: argparse.Namespace) -> Params:
    overrides: dict[str, Any] = {}
    if args.cap is not None:
        overrides["cap"] = args.cap
    if args.days is not None:
        overrides["days"] = args.days
    if args.seed is not None:
        overrides["seed"] = args.seed
    preset = presets()[args.preset]
    overrides.update(preset["params"])
    return Params(**overrides)


def _demand(args: argparse.Namespace) -> float:
    if args.demand is not None:
        return args.demand
    return presets()[args.preset]["demand_scale"]


def _print(payload: Any, table: bool = False) -> None:
    if table and isinstance(payload, list) and payload and isinstance(payload[0], dict):
        header = f"{'policy':<10}{'served':>9}{'held':>8}{'wait':>8}{'study':>8}{'welfare':>10}"
        print(header)
        print("-" * len(header))
        for row in payload:
            print(
                f"{row.get('key', ''):<10}"
                f"{row.get('served_rate', 0):>8.1%}"
                f"{row.get('empty_share', 0):>8.1%}"
                f"{row.get('wait', 0):>8.1f}"
                f"{row.get('avg_study_h', 0):>8.2f}"
                f"{row.get('welfare_day', 0):>10.0f}"
            )
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seatlab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"seatlab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--preset",
            choices=sorted(presets()),
            default="exam",
            help="calm term or exam week (default: exam)",
        )
        p.add_argument(
            "--demand", type=float, default=None, help="demand multiplier; overrides the preset"
        )
        p.add_argument("--cap", type=int, default=None, help="seats")
        p.add_argument("--days", type=int, default=None, help="days to average")
        p.add_argument("--seed", type=int, default=None, help="random seed")

    p_sim = sub.add_parser("simulate", help="run one mechanism")
    add_common(p_sim)
    p_sim.add_argument("--policy", choices=sorted(PRESET_POLICIES), default="fcfs")
    p_sim.add_argument("--table", action="store_true")

    p_cmp = sub.add_parser("compare", help="run every mechanism on the same population")
    add_common(p_cmp)
    p_cmp.add_argument("--policies", nargs="*", choices=sorted(PRESET_POLICIES), default=None)
    p_cmp.add_argument("--table", action="store_true")

    p_opt = sub.add_parser("optimize", help="grid search release × trigger")
    add_common(p_opt)
    p_opt.add_argument("--out", default=None, help="write JSON to this file")

    p_price = sub.add_parser("pricing", help="welfare along an hourly price")
    add_common(p_price)

    p_sc = sub.add_parser("scale-up", help="national seat-time ledger")
    p_sc.add_argument("--students-wan", type=float, default=4301.0)
    p_sc.add_argument("--universities", type=int, default=3119)
    p_sc.add_argument("--seats-lo", type=float, default=0.10)
    p_sc.add_argument("--seats-hi", type=float, default=0.14)
    p_sc.add_argument("--value-per-hour", type=float, default=2.6)

    p_val = sub.add_parser("validate", help="cross-check the queueing engine")
    p_val.add_argument("--n", type=int, default=200_000)

    p_study = sub.add_parser("study", help="reproduce the full published experiment suite")
    p_study.add_argument("--days", type=int, default=40)
    p_study.add_argument("--sweep-days", type=int, default=16)
    p_study.add_argument("--seed", type=int, default=20261121)
    p_study.add_argument("--out", default="results.json")

    p_cal = sub.add_parser(
        "calibrate", help="fit params to observed arrivals / empty share / study hours"
    )
    add_common(p_cal)
    p_cal.add_argument("--arrivals", type=float, default=None, help="observed arrivals per day")
    p_cal.add_argument("--empty", type=float, default=None, help="observed empty (held) share, 0-1")
    p_cal.add_argument(
        "--study-hours", type=float, default=None, help="observed mean hours per student"
    )
    p_cal.add_argument("--iterations", type=int, default=7, help="bisection steps (default 7)")

    p_sw = sub.add_parser("sweep", help="capacity x demand grid for one mechanism")
    add_common(p_sw)
    p_sw.add_argument("--policy", choices=sorted(PRESET_POLICIES), default="fcfs")
    p_sw.add_argument("--caps", type=int, nargs="+", default=[380, 420, 460, 500, 540, 580, 620])
    p_sw.add_argument("--demands", type=float, nargs="+", default=[1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
    p_sw.add_argument("--csv", default=None, help="write the grid as CSV to this path")

    p_serve = sub.add_parser("serve", help="run the HTTP API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--workers", type=int, default=1)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.add_argument(
        "--db",
        default=None,
        help="SQLite file or PostgreSQL DSN (or set SEATLAB_DB); default :memory:",
    )
    p_serve.add_argument(
        "--auth-mode",
        choices=["header", "cas", "oauth"],
        default=None,
        help="override SEATLAB_AUTH_MODE for this process",
    )
    p_serve.add_argument("--public-url", default=None, help="public base URL for SSO callbacks")
    p_serve.add_argument("--admin-key", default=None, help="require X-Admin-Key on /api/v1/admin/*")
    p_serve.add_argument("--app-key", default=None, help="require X-App-Key on student writes")

    p_init = sub.add_parser("init-db", help="create the operational database and seat inventory")
    p_init.add_argument(
        "--db", required=True, help="SQLite file or PostgreSQL DSN to create/migrate"
    )
    p_init.add_argument("--name", default=None, help="venue name")
    p_init.add_argument("--cap", type=int, default=None, help="seats (10-5000)")

    p_demo = sub.add_parser("demo", help="seed a demo venue with reservations, reports and notices")
    p_demo.add_argument("--db", required=True)
    p_demo.add_argument(
        "--students", type=int, default=24, help="how many sample students to create"
    )

    p_status = sub.add_parser("ops-status", help="print the admin dashboard as JSON")
    p_status.add_argument("--db", required=True)

    p_auth = sub.add_parser(
        "auth-status", help="show SSO config, storage backend and live sessions"
    )
    p_auth.add_argument("--db", default=None, help="defaults to SEATLAB_DB or :memory:")
    p_auth.add_argument("--json", action="store_true", help="machine-readable output")

    p_sessions = sub.add_parser("sessions", help="list login sessions (hashes only, never tokens)")
    p_sessions.add_argument("--db", required=True)
    p_sessions.add_argument("--all", action="store_true", help="include revoked sessions")
    p_sessions.add_argument("--limit", type=int, default=50)

    p_revoke = sub.add_parser("revoke-sessions", help="revoke sessions for a student or everyone")
    p_revoke.add_argument("--db", required=True)
    group = p_revoke.add_mutually_exclusive_group(required=True)
    group.add_argument("--student", help="campus account id")
    group.add_argument("--all", action="store_true", help="revoke every active session")
    p_revoke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "simulate":
            params = _params(args)
            result = run(params, resolve_policy(args.policy), _demand(args), params.days)
            _print([result] if args.table else result, table=args.table)
            return 0

        if args.command == "compare":
            params = _params(args)
            rows = compare(params, args.policies, _demand(args), params.days)
            _print(rows, table=args.table)
            return 0

        if args.command == "optimize":
            params = _params(args)
            result = optimize(params, _demand(args), params.days or 16)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    json.dump(result, fh, ensure_ascii=False, indent=2)
                print(f"wrote {args.out} ({len(result['rows'])} cells)", file=sys.stderr)
            _print(result)
            return 0

        if args.command == "pricing":
            params = _params(args)
            _print(pricing_curve(params, _demand(args), params.days or 16))
            return 0

        if args.command == "scale-up":
            _print(
                scale_up(
                    students_wan=args.students_wan,
                    universities=args.universities,
                    seats_per_student=(args.seats_lo, args.seats_hi),
                    value_per_hour=args.value_per_hour,
                )
            )
            return 0

        if args.command == "calibrate":
            if args.arrivals is None and args.empty is None and args.study_hours is None:
                print(
                    "error: give at least one of --arrivals / --empty / --study-hours",
                    file=sys.stderr,
                )
                return 2
            _print(
                calibrate(
                    _params(args),
                    arrivals_per_day=args.arrivals,
                    empty_share=args.empty,
                    mean_study_h=args.study_hours,
                    demand_scale=_demand(args),
                    days=args.days or 6,
                    iterations=args.iterations,
                )
            )
            return 0

        if args.command == "sweep":
            result = sweep(
                _params(args),
                args.policy,
                caps=args.caps,
                demands=args.demands,
                days=args.days or 8,
            )
            if args.csv:
                keys = [
                    "cap",
                    "demand",
                    "served_rate",
                    "empty_share",
                    "wait",
                    "util",
                    "peak_util",
                    "welfare_day",
                ]
                with open(args.csv, "w", encoding="utf-8") as fh:
                    fh.write(",".join(keys) + "\n")
                    for row in result["rows"]:
                        fh.write(",".join(str(row[k]) for k in keys) + "\n")
                print(f"wrote {args.csv}", file=sys.stderr)
            _print(result)
            return 0

        if args.command == "validate":
            _print(validate_queueing(n=args.n))
            return 0

        if args.command == "study":
            result = run_study(days=args.days, sweep_days=args.sweep_days, seed=args.seed)
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
            print(f"wrote {args.out}", file=sys.stderr)
            return 0

        if args.command == "init-db":
            from .service import OpsService
            from .store import Store

            patch = {}
            if args.name:
                patch["name"] = args.name
            if args.cap:
                patch["cap"] = args.cap
            store = Store(args.db)
            service = OpsService(store)
            if patch:
                service.update_venue(patch)
            _print(service.venue())
            return 0

        if args.command == "demo":
            from .service import OpsService
            from .store import Store

            store = Store(args.db)
            service = OpsService(store)
            print(json.dumps(_seed_demo(service, args.students), ensure_ascii=False, indent=2))
            return 0

        if args.command == "ops-status":
            from .service import OpsService
            from .store import Store

            store = Store(args.db)
            _print(OpsService(store).dashboard())
            store.close()
            return 0

        if args.command == "auth-status":
            from .auth import AuthConfig
            from .store import Store

            store = Store(args.db or os.getenv("SEATLAB_DB") or ":memory:")
            cfg = AuthConfig.from_env()
            now_iso = datetime.now().isoformat(timespec="seconds")
            sessions = store.sessions(active_only=True, limit=10_000, now_iso=now_iso)
            payload = {
                "storage": store.backend,
                "schema_version": store.schema_version,
                "auth": cfg.describe(),
                "active_sessions": len(sessions),
                "students_with_sessions": len({s["student_id"] for s in sessions}),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"storage      {payload['storage']} (schema v{payload['schema_version']})")
                print(f"auth mode    {payload['auth']['mode']}")
                print(f"public url   {payload['auth']['public_url'] or '(derived from request)'}")
                if cfg.mode == "cas":
                    print(f"CAS base     {cfg.cas_base}")
                    print(f"CAS validate {cfg.cas_validate}")
                if cfg.mode == "oauth":
                    print(f"OAuth authz  {cfg.oauth_authorize}")
                    print(f"OAuth info   {cfg.oauth_userinfo}")
                print(
                    f"admin SSO    {payload['auth']['admin_sso']} ({payload['auth']['admin_ids']} ids)"
                )
                print(
                    f"sessions     {payload['active_sessions']} active / "
                    f"{payload['students_with_sessions']} students"
                )
            store.close()
            return 0

        if args.command == "sessions":
            from .store import Store

            store = Store(args.db)
            now_iso = datetime.now().isoformat(timespec="seconds")
            rows = store.sessions(
                active_only=not args.all, limit=args.limit, now_iso=None if args.all else now_iso
            )
            _print(
                [
                    {
                        "token": r["token_hash"][:12] + "…",
                        "student": r["student_id"],
                        "role": r["role"],
                        "name": r["name"] or "",
                        "created": r["created_at"],
                        "expires": r["expires_at"],
                        "last_seen": r["last_seen_at"],
                        "revoked": bool(r["revoked"]),
                    }
                    for r in rows
                ]
            )
            store.close()
            return 0

        if args.command == "revoke-sessions":
            from .store import Store

            store = Store(args.db)
            if args.all:
                rows = store.sessions(active_only=True, limit=10_000)
                if not args.yes:
                    print(
                        f"about to revoke {len(rows)} active sessions; pass --yes", file=sys.stderr
                    )
                    store.close()
                    return 2
                count = sum(store.revoke_session(r["token_hash"]) for r in rows)
            else:
                count = store.revoke_student_sessions(args.student)
            print(json.dumps({"revoked": count}, indent=2))
            store.close()
            return 0

        if args.command == "serve":
            if args.db:
                os.environ["SEATLAB_DB"] = args.db
            if args.admin_key:
                os.environ["SEATLAB_ADMIN_KEY"] = args.admin_key
            if args.app_key:
                os.environ["SEATLAB_APP_KEY"] = args.app_key
            if args.auth_mode:
                os.environ["SEATLAB_AUTH_MODE"] = args.auth_mode
            if args.public_url:
                os.environ["SEATLAB_PUBLIC_URL"] = args.public_url
            try:
                import uvicorn
            except ImportError:
                print("uvicorn is missing: pip install 'seatlab[api]'", file=sys.stderr)
                return 2
            uvicorn.run(
                "seatlab.api.app:get_app",
                factory=True,
                host=args.host,
                port=args.port,
                workers=args.workers,
                reload=args.reload,
            )
            return 0

    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 1


def _seed_demo(service, students: int) -> dict[str, Any]:
    """Create a deterministic demo scene for the admin and student pages.

    The scene is built to look like a real mid-morning: most reservations are
    checked in, a few are away, a couple never showed up (expired + credit
    points) and two seats were reported by other students.
    """
    from .service import OpsError

    zones = [z["key"] for z in service.venue()["zones"]]
    created = checked = away = 0
    for i in range(students):
        sid = f"demo-{i + 1:03d}"
        try:
            res = service.reserve(sid, zone=zones[i % len(zones)], minutes=180)
        except OpsError:
            break
        created += 1
        if i < students - 4:
            service.checkin(res["id"], sid, res["code"])
            checked += 1
            if i % 7 == 0:
                service.away(res["id"], sid, 15)
                away += 1

    expired = 0
    for res in service.store.reservations(status="reserved", limit=2):
        service.store.update_reservation(int(res["id"]), status="expired", reason="demo_no_show")
        service._free(int(res["seat_id"]))
        service._violate(res["student_id"], int(res["id"]), "no_show", service.now(), "demo")
        expired += 1

    reported = 0
    for res in service.store.reservations(status="reserved", limit=2):
        service.report(
            f"demo-reporter-{reported + 1}", int(res["seat_id"]), "座位上只有书，人一直不在"
        )
        reported += 1

    service.announce("期末周延时闭馆", "本周五起闭馆时间延后至 23:00，请按预约时段使用座位。")
    return {
        "students": created,
        "checked_in": checked,
        "away": away,
        "expired": expired,
        "reported": reported,
        "complaints": len(service.store.complaints()),
        "dashboard": service.dashboard()["today"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
