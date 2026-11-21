"""Closed form and independent-simulation queueing checks."""

from __future__ import annotations

import math

import pytest

from seatlab.queueing import erlang_c, mmc_wait, simulate_mmc
from seatlab.study import validate_queueing


def test_erlang_c_bounds_and_monotonicity() -> None:
    assert erlang_c(4, 0.0) == 0.0
    assert erlang_c(4, 4.0) == 1.0
    previous = 0.0
    for offered in (0.5, 1.0, 2.0, 3.0, 3.6):
        value = erlang_c(4, offered)
        assert previous <= value <= 1.0
        previous = value


def test_erlang_c_classic_mmc_case() -> None:
    # Textbook: c=4, A=3 => 13.5 / (13 + 13.5) = 0.50943
    assert erlang_c(4, 3.0) == pytest.approx(0.50943, abs=1e-4)
    # Single server: Erlang C collapses to rho.
    assert erlang_c(1, 0.8) == pytest.approx(0.8)


def test_erlang_c_survives_many_servers() -> None:
    # c! overflows a float at c=171; the log-domain version must not.
    value = erlang_c(500, 480.0)
    assert 0 < value < 1
    assert math.isfinite(value)


def test_mmc_wait_units_are_minutes() -> None:
    # One server, arrivals 1 every 2 minutes, service mean 1 minute.
    # Wq = rho / (mu - lam) = 0.5 / (1 - 0.5) = 1 minute.
    assert mmc_wait(1, 0.5, 1.0) == pytest.approx(1.0, rel=1e-9)


def test_mmc_wait_overload_is_infinite() -> None:
    assert mmc_wait(2, 3.0, 1.0) == float("inf")
    assert mmc_wait(2, 0.0, 1.0) == 0.0


def test_simulate_mmc_matches_theory() -> None:
    for c, rho, service_min in ((1, 0.5, 60.0), (6, 0.8, 69.0), (20, 0.75, 60.0)):
        mu = 1 / service_min
        lam = rho * c * mu
        theory = mmc_wait(c, lam, mu)
        simulated = simulate_mmc(c, lam, mu, n=80_000, seed=3)
        assert abs(simulated - theory) / theory < 0.08, (c, rho, simulated, theory)


def test_simulate_mmc_validates_arguments() -> None:
    with pytest.raises(ValueError):
        simulate_mmc(0, 1.0, 1.0)
    with pytest.raises(ValueError):
        simulate_mmc(2, 0.0, 1.0)
    assert simulate_mmc(1, 0.5, 1.0) == simulate_mmc(1, 0.5, 1.0)


def test_validation_table_stays_within_tolerance() -> None:
    rows = validate_queueing(n=60_000)
    assert len(rows) == 5
    for row in rows:
        assert row["abs_error_min"] <= 3.0, row
        # Relative error only matters once the queue is long enough to matter.
        if row["erlang_c_min"] >= 5.0:
            assert row["rel_error"] <= 0.12, row
