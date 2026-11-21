"""Engine invariants: conservation, determinism, pairing and a regression snapshot."""

from __future__ import annotations

import random

import pytest

from seatlab.engine import gini, run, simulate_day
from seatlab.model import Params, resolve_policy

POLICIES = ["fcfs", "t30", "t15", "smart", "patrol", "res", "mix", "credit", "price"]


def small_params(**kw) -> Params:
    base = dict(cap=120, days=1, seed=424242, open_minutes=720)
    base.update(kw)
    return Params(**base)


@pytest.mark.parametrize("key", POLICIES)
def test_audit_finds_no_counter_drift(key: str) -> None:
    params = small_params()
    day = simulate_day(params, resolve_policy(key), random.Random(params.seed), 1.4, audit=True)
    assert day["audit_errors"] == [], day["audit_errors"]


@pytest.mark.parametrize("key", POLICIES)
def test_seat_accounting_is_bounded(key: str) -> None:
    params = small_params()
    day = simulate_day(params, resolve_policy(key), random.Random(params.seed), 1.4)
    cap_hours = params.cap * params.open_minutes / 60
    assert 0 <= day["present_h"] <= cap_hours
    assert 0 <= day["held_h"] <= cap_hours
    assert 0 <= day["booked_h"] <= cap_hours
    assert 0 <= day["empty_share"] <= 1
    assert 0 <= day["util"] <= 1
    assert 0 < day["served"] <= day["arrivals"]
    assert day["util_profile"][0] >= 0


def test_served_never_exceeds_arrivals_over_many_days() -> None:
    params = small_params(days=6, cap=60)
    metrics = run(params, resolve_policy("res"), 1.4, params.days)
    assert metrics["served"] <= metrics["arrivals"]
    assert 0 <= metrics["served_rate"] <= 1
    assert metrics["gini"] <= 1


def test_run_is_deterministic() -> None:
    params = small_params(days=3, cap=80)
    policy = resolve_policy("smart")
    a = run(params, policy, 1.4, params.days)
    b = run(params, policy, 1.4, params.days)
    assert a == b


def test_different_seed_changes_outcomes() -> None:
    params = small_params(days=2, cap=80, seed=1)
    policy = resolve_policy("fcfs")
    a = run(params, policy, 1.4, params.days)
    b = run(params.replace(seed=2), policy, 1.4, params.days)
    assert a["arrivals"] != b["arrivals"]


def test_policies_share_the_same_population() -> None:
    """Paired random streams: arrivals must be identical across mechanisms."""
    params = small_params(days=4, cap=90)
    runs = {
        key: run(params, resolve_policy(key), 1.4, params.days)
        for key in ("fcfs", "t30", "smart", "patrol", "res", "mix", "credit", "price")
    }
    reference = runs["fcfs"]["arrivals_by_hour"]
    # Every mechanism except the priced one admits the whole population, so the
    # realised demand profile must be bit-identical across them.
    for key in ("t30", "smart", "patrol", "res", "mix", "credit"):
        assert runs[key]["arrivals_by_hour"] == reference, key
    # Pricing is the only mechanism that changes who arrives at all.
    assert runs["price"]["total_demand"] == runs["fcfs"]["total_demand"]
    assert runs["price"]["arrivals"] < runs["fcfs"]["arrivals"]


def test_queue_share_never_exceeds_one() -> None:
    """Queue minutes are counted per student, not per queue episode."""
    params = small_params(days=4, cap=90)
    for key in POLICIES:
        m = run(params, resolve_policy(key), 1.4, params.days)
        assert 0 <= m["queue_share"] <= 1, key
        assert m["wait_p50"] <= m["wait_p90"] <= params.patience_min + 1e-6, key


def test_wait_is_per_student_not_per_episode() -> None:
    """A student who queues twice must not be charged twice."""
    params = small_params(days=3, cap=80)
    m = run(params, resolve_policy("fcfs"), 1.4, params.days)
    # patience caps a single queue episode; total waits can only exceed it by
    # the number of episodes, so the mean must stay well below the cap.
    assert m["wait_mean_queued"] <= params.patience_min * 2 + 1e-6
    assert m["wait"] <= m["wait_mean_queued"] + 1e-6


def test_percentiles_are_ordered() -> None:
    params = small_params(days=3, cap=80)
    m = run(params, resolve_policy("smart"), 1.4, params.days)
    assert 0 <= m["study_p10"] <= m["study_p50"] <= m["avg_study_h"] + 1e-9


def test_price_policy_does_not_change_the_demand_stream() -> None:
    params = small_params(days=2, cap=90)
    free = run(params, resolve_policy("fcfs"), 1.4, params.days)
    paid = run(params, resolve_policy("price"), 1.4, params.days)
    assert free["total_demand"] == paid["total_demand"]
    assert paid["no_go"] > 0


def test_gini_known_values() -> None:
    assert gini([]) == 0.0
    assert gini([3.0, 3.0, 3.0]) == pytest.approx(0.0, abs=1e-12)
    assert gini([0.0, 10.0]) == pytest.approx(0.5, abs=1e-12)
    assert 0 < gini([0, 1, 2, 3, 100]) < 1


def test_regression_snapshot_exam_fcfs() -> None:
    """The published baseline: 500 seats, exam week, first-come-first-served.

    Any accidental change to the event loop, the random stream order or the
    welfare formula moves these numbers. They are the contract with the
    article and the demo page.
    """
    params = Params(days=40, mean_present_h=4.9)
    result = run(params, resolve_policy("fcfs"), 1.4, 40)
    assert result["welfare_day"] == pytest.approx(16123.07, rel=2e-3)
    assert result["served_rate"] == pytest.approx(0.8686, abs=2e-3)
    assert result["empty_share"] == pytest.approx(0.0883, abs=2e-3)
    assert result["held_h"] == pytest.approx(526.64, rel=2e-3)
    assert result["wait"] == pytest.approx(3.461, abs=0.3)
