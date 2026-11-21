"""Higher level experiments: grid search, pricing curve, scale-up, full study."""

from __future__ import annotations

import pytest

from seatlab.engine import run
from seatlab.model import Params, Policy, resolve_policy
from seatlab.study import (
    calibrate,
    compare,
    optimize,
    pricing_curve,
    run_study,
    scale_up,
    sweep,
)

SMALL = Params(cap=140, days=2, seed=99, open_minutes=720)


def test_compare_reuses_one_population() -> None:
    rows = compare(SMALL, ["fcfs", "t30"], 1.2, 2)
    assert [r["key"] for r in rows] == ["fcfs", "t30"]
    assert rows[0]["arrivals_by_hour"] == rows[1]["arrivals_by_hour"]


def test_compare_default_covers_every_mechanism() -> None:
    rows = compare(SMALL, None, 1.2, 2)
    assert len(rows) == 9
    assert {r["key"] for r in rows} >= {"fcfs", "smart", "res", "price"}


def test_optimize_grid_is_complete_and_best_is_the_max() -> None:
    result = optimize(SMALL, 1.4, 2, release_values=(15, 30), trigger_values=(0.9, 1.0))
    rows = result["rows"]
    assert len(rows) == 4
    assert {(r["release"], r["trigger"]) for r in rows} == {
        (15, 0.9),
        (15, 1.0),
        (30, 0.9),
        (30, 1.0),
    }
    assert result["best_welfare"]["welfare_day"] == max(r["welfare_day"] for r in rows)
    assert result["best_service"]["served_rate"] == max(r["served_rate"] for r in rows)
    for row in rows:
        assert 0 <= row["served_rate"] <= 1
        assert row["welfare_day"] > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"release_values": ()},
        {"trigger_values": [0.5] * 12},
        {"release_values": list(range(1, 26))},
    ],
)
def test_optimize_rejects_degenerate_grids(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        optimize(SMALL, 1.4, 2, **kwargs)


def test_pricing_curve_reports_the_best_price() -> None:
    curve = pricing_curve(SMALL, 1.4, 2, prices=(0.0, 1.0, 2.0))
    rows = curve["rows"]
    assert [r["price"] for r in rows] == [0.0, 1.0, 2.0]
    assert curve["best"]["welfare_day"] == max(r["welfare_day"] for r in rows)
    assert curve["gain_vs_free_pct"] is not None
    # Free riding is the price-0 point and must equal the fcfs baseline.
    free = rows[0]
    base = run(SMALL, resolve_policy("fcfs"), 1.4, 2)
    assert free["welfare_day"] == pytest.approx(round(base["welfare_day"], 1), abs=0.2)
    # Higher prices crowd out more demand.
    assert rows[2]["crowd_out"] > rows[1]["crowd_out"] > rows[0]["crowd_out"] >= 0


def test_pricing_curve_rejects_too_many_points() -> None:
    with pytest.raises(ValueError):
        pricing_curve(SMALL, 1.4, 2, prices=[float(i) for i in range(13)])


def test_scale_up_matches_the_published_ledger() -> None:
    """15.0-21.0 亿小时 of held-but-empty seat time (old results.json: 14.98-20.97)."""
    national = scale_up()
    assert national["wasted_hours_yi"] == pytest.approx([14.98, 20.97], abs=0.05)
    assert national["yuan_yi"] == pytest.approx([38.9, 54.5], abs=0.2)
    assert national["seats_wan"] == pytest.approx([430.1, 602.1], abs=0.1)
    assert 30 < national["hours_per_student_year"][0] < 55


def test_scale_up_is_monotone_in_seats() -> None:
    low = scale_up(seats_per_student=(0.10, 0.11))
    high = scale_up(seats_per_student=(0.13, 0.14))
    assert low["wasted_hours_yi"][0] < high["wasted_hours_yi"][0]
    assert low["yuan_yi"][0] < low["yuan_yi"][1]


def test_scale_up_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        scale_up(students_wan=0)
    with pytest.raises(ValueError):
        scale_up(seats_per_student=(0.14, 0.10))


def test_full_study_has_the_documented_shape() -> None:
    study = run_study(days=3, sweep_days=1, seed=7, validation_n=20_000)
    assert set(study) >= {
        "validation",
        "scenarios",
        "heatmap",
        "capacity",
        "shadow_price",
        "equivalent",
        "optimizer",
        "pricing",
        "price_sensitivity",
        "national",
        "meta",
    }
    assert len(study["validation"]) == 5
    assert [s["tag"] for s in study["scenarios"]] == ["calm", "exam"]
    assert all(len(s["rows"]) == 9 for s in study["scenarios"])
    assert len(study["heatmap"]["cells"]) == 6 * 5
    assert len(study["capacity"]["rows"]) == 4 * 7
    assert len(study["optimizer"]) == 8 * 5
    assert len(study["pricing"]) == 9
    assert len(study["price_sensitivity"]) == 12
    assert study["shadow_price"]["yuan_per_seat_day"] > 0
    assert study["meta"]["days_scenario"] == 3


def test_full_study_is_reproducible() -> None:
    a = run_study(days=2, sweep_days=1, seed=11, validation_n=5_000)
    b = run_study(days=2, sweep_days=1, seed=11, validation_n=5_000)
    assert a == b


def test_custom_policy_price_zero_matches_free() -> None:
    zero = Policy(key="zero", name="free", price=0.0)
    paid_zero = run(SMALL, zero, 1.4, 2)
    free = run(SMALL, resolve_policy("fcfs"), 1.4, 2)
    assert paid_zero["welfare_day"] == pytest.approx(free["welfare_day"], rel=1e-9)
    assert paid_zero["revenue_day"] == 0.0


def test_calibrate_beats_the_naive_guess() -> None:
    truth = Params(days=4, seed=5150, mean_present_h=5.4, hoard_mu=1.8, visits_per_seat=1.8)
    obs = run(truth, resolve_policy("fcfs"), 1.2, 4)
    fit = calibrate(
        Params(days=4, seed=5150),
        arrivals_per_day=obs["arrivals"],
        empty_share=obs["empty_share"],
        mean_study_h=obs["avg_study_h"],
        demand_scale=1.2,
        days=4,
        iterations=5,
    )
    got = fit["params"]
    assert fit["saturated"] == []
    assert abs(got["visits_per_seat"] - 1.8) / 1.8 < 0.2
    assert abs(got["mean_present_h"] - 5.4) / 5.4 < 0.2
    assert abs(fit["residuals"]["mean_study_h_abs"]) < 0.15


def test_calibrate_flags_an_impossible_target() -> None:
    fit = calibrate(
        Params(days=4, seed=1),
        arrivals_per_day=9000.0,  # > 10 visits per seat: outside the fitted box
        demand_scale=1.0,
        days=4,
        iterations=4,
    )
    assert "visits_per_seat" in fit["saturated"]


def test_sweep_is_bounded_and_ranked() -> None:
    out = sweep(Params(days=5), caps=[300, 500], demands=[1.1, 1.4], days=5)
    assert len(out["rows"]) == 4
    assert out["best"]["welfare_day"] == max(c["welfare_day"] for c in out["rows"])
    with pytest.raises(ValueError, match="1..10"):
        sweep(Params(days=5), caps=[], demands=[1.2], days=5)
