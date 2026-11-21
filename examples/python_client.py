"""Minimal client for the SeatLab API — standard library only.

    python examples/python_client.py                       # defaults to localhost:8000
    SEATLAB_API_KEY=secret python examples/python_client.py http://lib.example.edu.cn

It prints a table of the nine mechanisms on the same population, then runs a
capacity/demand sweep, a calibration from synthetic observations and the
seat-time ledger extrapolation.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
KEY = os.getenv("SEATLAB_API_KEY", "")


def call(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method="GET" if data is None else "POST",
        headers={"Content-Type": "application/json", **({"X-API-Key": KEY} if KEY else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise SystemExit(f"{path} -> HTTP {exc.code}: {body}") from exc


def main() -> None:
    health = call("/healthz")
    print(f"seatlab {health['version']} on {BASE}\n")

    compared = call(
        "/api/v1/compare",
        {
            "params": {"cap": 500, "days": 16, "mean_present_h": 4.9},
            "demand_scale": 1.4,
        },
    )
    base = compared["base_key"]
    print(f"{'mechanism':<9}{'served':>8}{'held':>8}{'wait':>8}{'welfare':>10}{'vs base':>9}")
    for row in compared["rows"]:
        gain = compared["welfare_gain_pct"][row["key"]]
        print(
            f"{row['key']:<9}{row['served_rate']:>8.1%}{row['empty_share']:>8.1%}"
            f"{row['wait']:>8.1f}{row['welfare_day']:>10.0f}{gain:>+8.1f}%"
        )
    print(f"\nbaseline = {base}; best welfare = {compared['best_welfare_key']}")

    swept = call(
        "/api/v1/sweep",
        {"policy": "t15", "caps": [420, 500, 580], "demands": [1.2, 1.4], "days": 8},
    )
    best = swept["best"]
    print(
        f"\nsweep {swept['key']}: {len(swept['rows'])} cells, best cap={best['cap']}"
        f" demand={best['demand']} welfare={best['welfare_day']:.0f}"
    )

    observed = call(
        "/api/v1/simulate",
        {"params": {"days": 5, "seed": 889, "mean_present_h": 5.0}, "demand_scale": 1.25},
    )["metrics"]
    fitted = call(
        "/api/v1/calibrate",
        {
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
    p = fitted["params"]
    print(
        f"\ncalibrated: visits_per_seat={p['visits_per_seat']:.3f}"
        f" mean_present_h={p['mean_present_h']:.2f} hoard_mu={p['hoard_mu']:.2f}"
        f" runs={fitted['runs']}"
    )

    ledger = call("/api/v1/scale-up", {"students_wan": 4301, "universities": 3119})
    lo, hi = ledger["wasted_hours_yi"]
    print(f"\nheld-but-empty seat time, national: {lo:.1f}–{hi:.1f} 亿 hours/year")


if __name__ == "__main__":
    main()
