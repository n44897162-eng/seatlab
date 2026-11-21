"""Higher-level experiments built on top of :func:`seatlab.engine.run`.

Everything here is deterministic: same seed, same params, same numbers.
The functions return plain JSON-serialisable structures so the CLI, the API
and the report all consume exactly one implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .engine import run
from .model import PRESET_POLICIES, Params, Policy, resolve_policy
from .queueing import mmc_wait, simulate_mmc

DEFAULT_POLICIES = ("fcfs", "t30", "t15", "smart", "patrol", "res", "mix", "credit", "price")


def _clean(value: Any) -> Any:
    """Round floats for stable JSON output without touching integers."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def compare(
    params: Params,
    policies: Iterable[str] | None = None,
    demand_scale: float = 1.0,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Run several mechanisms against the *same* population and random stream."""
    keys = list(policies or DEFAULT_POLICIES)
    return [run(params, resolve_policy(k), demand_scale, days) for k in keys]


def optimize(
    params: Params,
    demand_scale: float = 1.4,
    days: int = 16,
    release_values: Iterable[int] = (10, 15, 20, 25, 30, 40, 50, 60),
    trigger_values: Iterable[float] = (0.75, 0.85, 0.90, 0.95, 1.00),
) -> dict[str, Any]:
    """Grid search over ``release`` minutes × occupancy ``trigger``.

    Returns the full grid plus the welfare-maximising point and the
    service-rate-maximising point, which are usually not the same cell.
    """
    releases = [int(v) for v in release_values]
    triggers = [float(v) for v in trigger_values]
    if not 1 <= len(releases) <= 20:
        raise ValueError("release_values must contain 1..20 entries")
    if not 1 <= len(triggers) <= 10:
        raise ValueError("trigger_values must contain 1..10 entries")
    if len(releases) * len(triggers) > 120:
        raise ValueError("grid is too large: keep release×trigger <= 120")

    rows: list[dict[str, Any]] = []
    for t in releases:
        for tr in triggers:
            policy = Policy(
                key=f"opt_{t}_{tr:g}", name=f"T={t} min, trigger={tr:.0%}", release=t, trigger=tr
            )
            metrics = run(params, policy, demand_scale, days)
            rows.append(
                {
                    "release": t,
                    "trigger": tr,
                    "served_rate": round(metrics["served_rate"], 4),
                    "welfare_day": round(metrics["welfare_day"], 1),
                    "empty_share": round(metrics["empty_share"], 4),
                    "wait": round(metrics["wait"], 2),
                    "displaced": round(metrics["displaced"], 0),
                    "avg_study_h": round(metrics["avg_study_h"], 3),
                }
            )

    best_welfare = max(rows, key=lambda r: r["welfare_day"])
    best_service = max(rows, key=lambda r: r["served_rate"])
    return {
        "rows": rows,
        "best_welfare": best_welfare,
        "best_service": best_service,
        "demand_scale": demand_scale,
        "days": days,
    }


def pricing_curve(
    params: Params,
    demand_scale: float = 1.4,
    days: int = 16,
    prices: Iterable[float] = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0),
) -> dict[str, Any]:
    """Welfare / equity / revenue along an hourly seat price."""
    values = [float(v) for v in prices]
    if not 1 <= len(values) <= 12:
        raise ValueError("prices must contain 1..12 entries")
    rows = []
    for price in values:
        policy = Policy(key=f"price_{price:g}", name=f"{price:g} CNY/h", price=price)
        m = run(params, policy, demand_scale, days)
        rows.append(
            {
                "price": price,
                "welfare_day": round(m["welfare_day"], 1),
                "gini": round(m["gini"], 3),
                "avg_study_h": round(m["avg_study_h"], 3),
                "served_rate": round(m["served_rate"], 4),
                "revenue_day": round(m["revenue_day"], 1),
                "crowd_out": round(m["no_go"] / max(1.0, m["total_demand"]), 4),
                "wait": round(m["wait"], 2),
            }
        )
    best = max(rows, key=lambda r: r["welfare_day"])
    free = next((r for r in rows if r["price"] == 0), None)
    gain = None
    if free and free["welfare_day"] > 0:
        gain = round((best["welfare_day"] / free["welfare_day"] - 1) * 100, 2)
    return {"rows": rows, "best": best, "gain_vs_free_pct": gain}


def scale_up(
    students_wan: float = 4301.0,
    universities: int = 3119,
    seats_per_student: tuple[float, float] = (0.10, 0.14),
    peak_days: int = 110,
    calm_days: int = 170,
    open_hours: float = 14.0,
    empty_exam: float = 0.087,
    empty_calm: float = 0.090,
    value_per_hour: float = 2.6,
) -> dict[str, Any]:
    """Extrapolate the seat-time that sits empty-but-held to a national ledger.

    This is an order-of-magnitude estimate, not a statistic: every assumption
    is an explicit argument so a reader can re-run it with their own numbers.
    """
    if students_wan <= 0:
        raise ValueError("students_wan must be positive")
    lo_seats_per, hi_seats_per = seats_per_student
    if not 0 < lo_seats_per <= hi_seats_per:
        raise ValueError("seats_per_student must be an increasing positive pair")

    students = students_wan * 1e4
    seats_lo = students * lo_seats_per
    seats_hi = students * hi_seats_per

    def wasted(seats: float) -> float:
        return seats * (empty_exam * peak_days + empty_calm * calm_days) * open_hours

    low, high = wasted(seats_lo), wasted(seats_hi)
    return {
        "students_wan": round(students_wan, 2),
        "universities": universities,
        "seats_wan": [round(seats_lo / 1e4, 1), round(seats_hi / 1e4, 1)],
        "empty_exam": empty_exam,
        "empty_calm": empty_calm,
        "wasted_hours_yi": [round(low / 1e8, 2), round(high / 1e8, 2)],
        "yuan_yi": [round(low * value_per_hour / 1e8, 1), round(high * value_per_hour / 1e8, 1)],
        "hours_per_student_year": [
            round(low / students, 1),
            round(high / students, 1),
        ],
        "assumptions": {
            "seats_per_student": [lo_seats_per, hi_seats_per],
            "peak_days": peak_days,
            "calm_days": calm_days,
            "open_hours": open_hours,
            "value_per_hour": value_per_hour,
        },
    }


def validate_queueing(n: int = 200_000, seed: int = 9) -> list[dict[str, Any]]:
    """Cross-check the independent M/M/c simulator against the Erlang-C formula.

    Seats are servers, so the same check validates the "seat = server" mapping
    used by the main engine: identical rate units, identical queue discipline.
    """
    cases = ((1, 0.50, 60.0), (6, 0.80, 69.0), (8, 0.85, 66.0), (20, 0.75, 60.0), (60, 0.90, 55.0))
    out = []
    for c, rho, service_min in cases:
        mu = 1.0 / service_min
        lam = rho * c * mu
        theory = mmc_wait(c, lam, mu)
        simulated = simulate_mmc(c, lam, mu, n=n, seed=seed)
        out.append(
            {
                "c": c,
                "rho": round(rho, 3),
                "mean_service_min": service_min,
                "erlang_c_min": round(theory, 2),
                "simulated_min": round(simulated, 2),
                "abs_error_min": round(abs(simulated - theory), 2),
                "rel_error": round(abs(simulated - theory) / max(1e-9, theory), 4),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Calibration: fit the model to what a library already observes
# --------------------------------------------------------------------------- #
def calibrate(
    params: Params,
    *,
    arrivals_per_day: float | None = None,
    empty_share: float | None = None,
    mean_study_h: float | None = None,
    demand_scale: float = 1.0,
    days: int = 6,
    hoard_kappa: float = 1.4,
    iterations: int = 7,
) -> dict[str, Any]:
    """Fit the three hard-to-observe inputs to three observable outputs.

    A library can measure how many students arrive, how long they study and how
    much of the occupied seat-time is empty. Those three numbers pin down
    ``visits_per_seat``, ``mean_present_h`` and the mean hoarding propensity.
    The fit is a deterministic bisection on each axis, not an optimiser: the
    same targets always give the same parameters.

    Targets use the *current* mechanism (fcfs), because that is what a library
    observes before changing anything.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if iterations < 3:
        raise ValueError("iterations must be at least 3")
    for name, value, lo, hi in (
        ("empty_share", empty_share, 0.0, 0.99),
        ("mean_study_h", mean_study_h, 0.05, 10.0),
    ):
        if value is not None and not lo <= value <= hi:
            raise ValueError(f"{name} target must be within [{lo}, {hi}]")
    if arrivals_per_day is not None and arrivals_per_day <= 0:
        raise ValueError("arrivals_per_day must be positive")

    policy = PRESET_POLICIES["fcfs"]
    fitted = params.replace(days=days)
    trace: list[dict[str, Any]] = []
    runs = 0

    def measure(p: Params) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        return run(p, policy, demand_scale, days)

    if arrivals_per_day is not None:
        # Arrivals scale linearly with visits_per_seat, so solve it in one step
        # and then correct for rounding/jitter with a second pass.
        current = measure(fitted)
        ratio = arrivals_per_day / max(1.0, current["arrivals"])
        vps = fitted.visits_per_seat * ratio
        fitted = fitted.replace(visits_per_seat=min(10.0, max(0.05, vps)))
        trace.append({"step": "visits_per_seat", "value": round(fitted.visits_per_seat, 3)})

    if mean_study_h is not None:
        lo, hi = 1.0, min(12.0, params.open_minutes / 60 - 0.5)
        for _ in range(iterations):
            mid = (lo + hi) / 2
            trial = fitted.replace(mean_present_h=mid)
            m = measure(trial)
            if m["avg_study_h"] < mean_study_h:
                lo = mid
            else:
                hi = mid
        fitted = fitted.replace(mean_present_h=round((lo + hi) / 2, 3))
        trace.append({"step": "mean_present_h", "value": fitted.mean_present_h})

    if empty_share is not None:
        lo, hi = 0.02, 0.95  # bounds on the *mean* hoarding propensity
        for _ in range(iterations):
            mid = (lo + hi) / 2
            mu = hoard_kappa * mid / (1 - mid)
            trial = fitted.replace(hoard_mu=mu)
            m = measure(trial)
            if m["empty_share"] < empty_share:
                lo = mid
            else:
                hi = mid
        mean_hoard = (lo + hi) / 2
        mu = hoard_kappa * mean_hoard / (1 - mean_hoard)
        fitted = fitted.replace(hoard_mu=round(mu, 3))
        trace.append({"step": "hoard_mean", "value": round(mean_hoard, 3)})

    # One corrective pass: changing the stay length also changes arrivals a bit.
    final = measure(fitted)
    if arrivals_per_day is not None:
        ratio = arrivals_per_day / max(1.0, final["arrivals"])
        if abs(ratio - 1) > 0.01:
            fitted = fitted.replace(
                visits_per_seat=min(10.0, max(0.05, fitted.visits_per_seat * ratio))
            )
            final = measure(fitted)

    residuals = {}
    # An axis that could not reach its target means the target is outside the
    # feasible region (e.g. more arrivals than the doors can physically admit).
    # Report it instead of silently returning a best-effort corner.
    saturated: list[str] = []
    if arrivals_per_day is not None:
        residuals["arrivals_per_day_pct"] = round(
            (final["arrivals"] / arrivals_per_day - 1) * 100, 2
        )
    if empty_share is not None:
        residuals["empty_share_abs"] = round(abs(final["empty_share"] - empty_share), 4)
    if mean_study_h is not None:
        residuals["mean_study_h_abs"] = round(abs(final["avg_study_h"] - mean_study_h), 3)

    for residual_key, fitted_key, limit in (
        ("arrivals_per_day_pct", "visits_per_seat", 1.5),
        ("empty_share_abs", "hoard_mu", 0.01),
        ("mean_study_h_abs", "mean_present_h", 0.15),
    ):
        if residual_key in residuals and abs(residuals[residual_key]) > limit:
            saturated.append(fitted_key)

    return {
        "params": fitted.to_dict(),
        "achieved": {
            "arrivals": round(final["arrivals"], 1),
            "empty_share": round(final["empty_share"], 4),
            "avg_study_h": round(final["avg_study_h"], 3),
            "served_rate": round(final["served_rate"], 4),
            "welfare_day": round(final["welfare_day"], 1),
        },
        "targets": {
            "arrivals_per_day": arrivals_per_day,
            "empty_share": empty_share,
            "mean_study_h": mean_study_h,
        },
        "residuals": residuals,
        "saturated": saturated,
        "trace": trace,
        "runs": runs,
        "days": days,
        "demand_scale": demand_scale,
        "hoard_kappa": hoard_kappa,
    }


# --------------------------------------------------------------------------- #
# Sweep: capacity × demand heat map on one mechanism
# --------------------------------------------------------------------------- #
def sweep(
    params: Params,
    policy: str | Policy = "fcfs",
    *,
    caps: Iterable[int] = (380, 420, 460, 500, 540, 580, 620),
    demands: Iterable[float] = (1.0, 1.1, 1.2, 1.3, 1.4, 1.5),
    days: int = 8,
) -> dict[str, Any]:
    """Evaluate one mechanism over a capacity × demand grid."""
    cap_values = [int(c) for c in caps]
    demand_values = [float(d) for d in demands]
    if not 1 <= len(cap_values) <= 10:
        raise ValueError("caps must contain 1..10 entries")
    if not 1 <= len(demand_values) <= 10:
        raise ValueError("demands must contain 1..10 entries")
    if len(cap_values) * len(demand_values) > 80:
        raise ValueError("grid is too large: keep caps*demands <= 80")
    if any(c < 10 or c > 5000 for c in cap_values):
        raise ValueError("caps must be within [10, 5000]")
    if any(d <= 0 or d > 3 for d in demand_values):
        raise ValueError("demands must be within (0, 3]")

    chosen = resolve_policy(policy) if isinstance(policy, str) else policy
    rows = []
    for cap in cap_values:
        for demand in demand_values:
            m = run(params.replace(cap=cap), chosen, demand, days)
            rows.append(
                {
                    "cap": cap,
                    "demand": demand,
                    "served_rate": round(m["served_rate"], 4),
                    "empty_share": round(m["empty_share"], 4),
                    "wait": round(m["wait"], 2),
                    "util": round(m["util"], 4),
                    "peak_util": round(m["peak_util"], 4),
                    "welfare_day": round(m["welfare_day"], 1),
                }
            )
    best = max(rows, key=lambda r: r["welfare_day"])
    return {
        "key": chosen.key,
        "name": chosen.name,
        "caps": cap_values,
        "demands": demand_values,
        "days": days,
        "rows": rows,
        "best": best,
    }


# --------------------------------------------------------------------------- #
# The full study (regenerates the numbers used by the article and the demo page)
# --------------------------------------------------------------------------- #
def run_study(
    days: int = 40,
    sweep_days: int = 16,
    seed: int = 20261121,
    validation_n: int = 200_000,
) -> dict[str, Any]:
    """Reproduce the complete published experiment suite (~30-60 s)."""
    calm = Params(days=days, seed=seed, mean_present_h=4.2)
    exam = Params(days=days, seed=seed, mean_present_h=4.9)

    out: dict[str, Any] = {}
    out["validation"] = validate_queueing(n=validation_n, seed=9)

    scenarios = []
    for tag, params, demand in (("calm", calm, 1.0), ("exam", exam, 1.4)):
        rows = [run(params, PRESET_POLICIES[k], demand, days) for k in DEFAULT_POLICIES]
        scenarios.append(
            {
                "tag": tag,
                "demand_scale": demand,
                "rows": [_clean(r) for r in rows],
            }
        )
    out["scenarios"] = scenarios
    base_calm = scenarios[0]["rows"][0]
    base_exam = scenarios[1]["rows"][0]

    heat_rows = []
    for h in (0.0, 0.2, 0.4, 0.6, 0.8, 0.95):
        kappa = 1.4
        mu = kappa * h / (1 - h) if h < 1 else 1e4
        p = replace(exam, hoard_mu=max(mu, 1e-4), hoard_kappa=kappa, days=sweep_days)
        for demand in (1.0, 1.15, 1.30, 1.45, 1.60):
            r = run(p, PRESET_POLICIES["fcfs"], demand, sweep_days)
            heat_rows.append(
                {
                    "hoard": h,
                    "demand": demand,
                    "fail": round(r["fail_rate"], 4),
                    "wait": round(r["wait"], 2),
                    "empty": round(r["empty_share"], 4),
                    "served": round(r["served_rate"], 4),
                    "study": round(r["avg_study_h"], 3),
                    "welfare": round(r["welfare_day"], 1),
                }
            )
    out["heatmap"] = {
        "hoard": [0.0, 0.2, 0.4, 0.6, 0.8, 0.95],
        "demand": [1.0, 1.15, 1.30, 1.45, 1.60],
        "cells": heat_rows,
    }

    caps = (380, 420, 460, 500, 540, 580, 620)
    capacity_rows = []
    for key in ("fcfs", "t30", "t15", "smart"):
        policy = PRESET_POLICIES[key]
        for cap in caps:
            p = replace(exam, cap=cap, days=sweep_days)
            r = run(p, policy, 1.4, sweep_days)
            capacity_rows.append(
                {
                    "key": key,
                    "cap": cap,
                    "welfare": round(r["welfare_day"], 2),
                    "served": round(r["served_rate"], 4),
                    "wait": round(r["wait"], 2),
                    "present_h": round(r["present_h"], 1),
                    "empty": round(r["empty_share"], 4),
                }
            )
    out["capacity"] = {"caps": list(caps), "rows": capacity_rows}

    def welfare_at(key: str, cap: int) -> float:
        for row in capacity_rows:
            if row["key"] == key and row["cap"] == cap:
                return row["welfare"]
        raise KeyError((key, cap))

    marginal = (welfare_at("fcfs", 540) - welfare_at("fcfs", 460)) / 80.0
    out["shadow_price"] = {
        "yuan_per_seat_day": round(marginal, 2),
        "yuan_per_seat_year": round(marginal * 280, 0),
    }
    base_welfare = welfare_at("fcfs", 500)
    out["equivalent"] = {
        "marginal_welfare_per_seat": round(marginal, 2),
        "base_welfare": base_welfare,
        "seats": {
            key: round((welfare_at(key, 500) - base_welfare) / marginal, 1)
            for key in ("t30", "t15", "smart")
        },
    }

    opt = optimize(exam.replace(days=sweep_days), demand_scale=1.4, days=sweep_days)
    out["optimizer"] = opt["rows"]

    price = pricing_curve(exam.replace(days=sweep_days), demand_scale=1.4, days=sweep_days)
    out["pricing"] = price["rows"]

    sensitivity = []
    for alt in (0.2, 0.45, 0.7):
        for price_value in (0.0, 1.0, 1.5, 2.0):
            p = replace(exam, alt_value_frac=alt, days=sweep_days)
            r = run(
                p,
                Policy(key=f"ps_{alt}_{price_value}", name="sens", price=price_value),
                1.4,
                sweep_days,
            )
            sensitivity.append(
                {"alt": alt, "price": price_value, "welfare": round(r["welfare_day"], 1)}
            )
    out["price_sensitivity"] = sensitivity

    out["calib"] = {
        "cap": 500,
        "open_hours": 14,
        "visits_per_seat": 2.03,
        "util_calm": base_calm["util"],
        "util_exam": base_exam["util"],
        "peak_calm": base_calm["peak_util"],
        "peak_exam": base_exam["peak_util"],
        "empty_calm": base_calm["empty_share"],
        "empty_exam": base_exam["empty_share"],
    }

    out["national"] = scale_up(
        students_wan=3891.26 + 409.54,
        universities=3119,
        seats_per_student=(0.10, 0.14),
        empty_exam=base_exam["empty_share"],
        empty_calm=base_calm["empty_share"],
    )

    out["meta"] = {
        "engine": "seatlab",
        "seed": seed,
        "days_scenario": days,
        "days_sweep": sweep_days,
        "calm_mean_present_h": calm.mean_present_h,
        "exam_mean_present_h": exam.mean_present_h,
        "policies": list(DEFAULT_POLICIES),
    }
    return _clean(out)
