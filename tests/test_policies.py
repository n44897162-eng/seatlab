"""Behavioural tests: does each mechanism do what the paper claims?"""

from __future__ import annotations

import pytest

from seatlab.engine import run
from seatlab.model import Params, resolve_policy


def metrics(policy: str, demand: float, days: int = 4, mean_present_h: float = 4.9) -> dict:
    params = Params(days=days, seed=5, mean_present_h=mean_present_h)
    return run(params, resolve_policy(policy), demand, days)


@pytest.fixture(scope="module")
def exam() -> dict[str, dict]:
    return {
        k: metrics(k, 1.4)
        for k in ("fcfs", "t30", "smart", "patrol", "res", "mix", "credit", "price")
    }


@pytest.fixture(scope="module")
def calm() -> dict[str, dict]:
    return {k: metrics(k, 1.0, mean_present_h=4.2) for k in ("fcfs", "smart", "price")}


def test_every_metric_is_finite_and_in_range(exam: dict[str, dict]) -> None:
    for key, m in exam.items():
        assert 0 <= m["served_rate"] <= 1, key
        assert 0 <= m["empty_share"] <= 1, key
        assert 0 <= m["util"] <= 1 and 0 <= m["peak_util"] <= 1, key
        assert 0 <= m["gini"] <= 1, key
        assert m["wait"] >= 0 and m["avg_study_h"] >= 0, key
        assert all(v == v for v in m["util_profile"]), key


def test_release_30_reclaims_hoarded_seats(exam: dict[str, dict]) -> None:
    base, t30 = exam["fcfs"], exam["t30"]
    assert t30["empty_share"] < base["empty_share"]
    assert t30["displaced"] > 0


def test_release_15_is_stricter_than_release_30(exam: dict[str, dict]) -> None:
    t15 = metrics("t15", 1.4)
    assert t15["empty_share"] <= exam["t30"]["empty_share"] + 1e-9
    assert t15["displaced"] >= exam["t30"]["displaced"]


def test_smart_release_is_silent_in_a_calm_library(calm: dict[str, dict]) -> None:
    """The whole selling point: no rule fires while occupancy stays below 90%."""
    assert calm["smart"]["displaced"] == 0
    assert calm["smart"]["welfare_day"] == pytest.approx(calm["fcfs"]["welfare_day"], rel=1e-9)


def test_smart_release_still_bites_during_exam_week(exam: dict[str, dict]) -> None:
    assert exam["smart"]["displaced"] > 0
    assert exam["smart"]["welfare_day"] > exam["fcfs"]["welfare_day"]


def test_reservation_creates_booked_and_ghost_seat_time(exam: dict[str, dict]) -> None:
    res = exam["res"]
    assert res["booked_h"] > 0
    assert res["ghost_waste"] > 0
    assert res["wait"] > exam["fcfs"]["wait"]


def test_mix_combines_both_effects(exam: dict[str, dict]) -> None:
    mix = exam["mix"]
    assert mix["booked_h"] > 0
    assert mix["empty_share"] < exam["fcfs"]["empty_share"]


def test_patrol_clears_seats_without_a_timer(exam: dict[str, dict]) -> None:
    patrol = exam["patrol"]
    assert patrol["patrol_events"] > 0
    assert patrol["displaced"] > 0
    assert patrol["empty_share"] < exam["fcfs"]["empty_share"]
    assert patrol["served_rate"] > exam["fcfs"]["served_rate"]


def test_credit_reduces_hoarding(exam: dict[str, dict]) -> None:
    assert exam["credit"]["empty_share"] < exam["fcfs"]["empty_share"]
    assert exam["credit"]["displaced"] > 0


def test_price_clears_the_queue_and_earns_revenue(exam: dict[str, dict]) -> None:
    price = exam["price"]
    assert price["no_go"] > 0
    assert price["revenue_day"] > 0
    assert price["served_rate"] > 0.99
    assert price["wait"] < 2.0  # the queue is effectively empty


def test_price_costs_welfare_when_the_library_is_empty(calm: dict[str, dict]) -> None:
    assert calm["price"]["welfare_day"] < calm["fcfs"]["welfare_day"]
    assert calm["price"]["no_go"] > 0


def test_welfare_decomposition_adds_up() -> None:
    params = Params(days=3, seed=5, mean_present_h=4.9)
    m = run(params, resolve_policy("price"), 1.4, 3)
    recomposed = m["study_value_day"] - m["wait_cost_day"] - m["monitor_day"]
    assert recomposed == pytest.approx(m["welfare_day"], rel=1e-9)
    # Revenue is a transfer, not welfare: it is reported but not added.
    assert m["revenue_day"] > 0
