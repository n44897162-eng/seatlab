"""Model validation and policy definitions."""

from __future__ import annotations

import dataclasses

import pytest

from seatlab.model import (
    PRESET_POLICIES,
    Params,
    Policy,
    presets,
    resolve_policy,
)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cap", 0),
        ("visits_per_seat", 0),
        ("mean_present_h", 20),
        ("open_minutes", 900),
        ("days", 0),
        ("alt_value_frac", 1.5),
        ("patience_min", 0),
    ],
)
def test_params_reject_invalid_values(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        Params(**{field: value})


def test_params_reject_stay_longer_than_opening() -> None:
    with pytest.raises(ValueError, match="mean_present_h"):
        Params(cap=100, mean_present_h=13, open_minutes=600)


def test_params_are_immutable() -> None:
    params = Params()
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.cap = 600  # type: ignore[misc]


def test_replace_returns_new_object() -> None:
    params = Params(cap=500)
    tighter = params.replace(cap=200)
    assert tighter.cap == 200 and params.cap == 500


@pytest.mark.parametrize(
    "overrides",
    [
        {"release": 0},
        {"reserve_share": 1.4},
        {"planner_share": -0.1},
        {"no_show": 2},
        {"trigger": 1.1},
        {"price": -1},
        {"checkin": -5},
        {"patrol_interval": -1},
        {"patrol_grace": -2},
    ],
)
def test_policy_rejects_invalid_values(overrides: dict) -> None:
    with pytest.raises(ValueError):
        Policy(**overrides)


def test_resolve_policy_preset_and_override() -> None:
    smart = resolve_policy("smart")
    assert smart.release == 30 and smart.trigger == pytest.approx(0.9)
    custom = resolve_policy("smart", release=20)
    assert custom.release == 20 and custom.trigger == pytest.approx(0.9)
    assert PRESET_POLICIES["smart"].release == 30


def test_resolve_policy_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown policy"):
        resolve_policy("nope")


def test_presets_are_copies() -> None:
    presets()["exam"]["params"]["mean_present_h"] = 99
    assert presets()["exam"]["params"]["mean_present_h"] != 99


def test_patrol_preset_is_a_real_sweep() -> None:
    patrol = PRESET_POLICIES["patrol"]
    assert patrol.staff_patrol and patrol.patrol_interval == 45 and patrol.patrol_grace == 10
    assert patrol.release is None


def test_policy_keys_are_stable() -> None:
    assert set(PRESET_POLICIES) == {
        "fcfs",
        "t30",
        "t15",
        "smart",
        "patrol",
        "res",
        "mix",
        "credit",
        "price",
    }
    assert PRESET_POLICIES["price"].price == 2.0
