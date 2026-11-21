"""Model inputs: :class:`Params` (the library and the population) and
:class:`Policy` (the allocation mechanism).

Both are frozen dataclasses so they can be used as cache keys. Validation is
done in ``__post_init__`` and raises ``ValueError`` with a human readable
message; the API layer turns those into HTTP 422 responses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .constants import DAY_MINUTES

PolicyKey = Literal["fcfs", "t30", "t15", "smart", "patrol", "res", "mix", "credit", "price"]


@dataclass(frozen=True)
class Params:
    """A single library under a single demand regime.

    All durations are minutes unless the name says ``_h``; money is CNY.
    """

    cap: int = 500
    visits_per_seat: float = 2.03
    mean_present_h: float = 4.20
    sigma_present: float = 0.55
    mean_away_min: float = 45.0
    sigma_away: float = 0.50
    n_away: float = 1.5
    n_short: float = 1.2
    n_class: float = 0.40
    hoard_mu: float = 2.0
    hoard_kappa: float = 1.4
    patience_min: float = 22.0
    rejoin_prob: float = 0.90
    value_median: float = 2.60
    value_sigma: float = 0.55
    wait_disu: float = 0.55
    monitor_cost_per_seat_day: float = 0.35
    alt_value_frac: float = 0.45
    open_minutes: int = DAY_MINUTES
    days: int = 30
    seed: int = 20261121
    daily_demand_jitter: float = 0.06

    def __post_init__(self) -> None:
        checks = [
            ("cap", 10, 5000),
            ("visits_per_seat", 0.05, 10),
            ("mean_present_h", 0.5, 13),
            ("sigma_present", 0.01, 2),
            ("mean_away_min", 5, 240),
            ("sigma_away", 0.01, 2),
            ("patience_min", 1, 240),
            ("value_median", 0.05, 50),
            ("value_sigma", 0.01, 2),
            ("alt_value_frac", 0, 1),
            ("open_minutes", 60, DAY_MINUTES),
            ("days", 1, 365),
        ]
        for name, lo, hi in checks:
            value = getattr(self, name)
            if not lo <= value <= hi:
                raise ValueError(f"params.{name} must be within [{lo}, {hi}], got {value}")
        for name in ("n_away", "n_short", "n_class", "hoard_mu", "hoard_kappa", "rejoin_prob"):
            if getattr(self, name) < 0:
                raise ValueError(f"params.{name} must be non-negative")
        if self.mean_present_h * 60 >= self.open_minutes:
            raise ValueError("mean_present_h must be shorter than the opening window")

    @property
    def expected_arrivals(self) -> int:
        return int(round(self.cap * self.visits_per_seat))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def replace(self, **kwargs) -> Params:
        return replace(self, **kwargs)


@dataclass(frozen=True)
class Policy:
    """One allocation mechanism.

    ``release``   minutes of unattended time before a held seat is reclaimed.
    ``trigger``   only start the release timer when occupancy ≥ trigger (0 = always).
    ``reserve_share`` fraction of seats that can be booked in advance.
    ``price``     hourly price in CNY; ``None`` means free.
    """

    key: str = "fcfs"
    name: str = "现状：先到先得"
    release: int | None = None
    reserve_share: float = 0.0
    checkin: int = 15
    no_show: float = 0.18
    planner_share: float = 0.0
    credit: bool = False
    credit_effect: float = 0.45
    price: float | None = None
    staff_patrol: bool = False
    patrol_interval: int = 45
    patrol_grace: int = 10
    trigger: float = 0.0

    def __post_init__(self) -> None:
        if self.release is not None and self.release <= 0:
            raise ValueError("policy.release must be positive when set")
        if not 0 <= self.reserve_share <= 1:
            raise ValueError("policy.reserve_share must be within [0, 1]")
        if not 0 <= self.planner_share <= 1:
            raise ValueError("policy.planner_share must be within [0, 1]")
        if not 0 <= self.no_show <= 1:
            raise ValueError("policy.no_show must be within [0, 1]")
        if not 0 <= self.trigger <= 1:
            raise ValueError("policy.trigger must be within [0, 1]")
        if self.price is not None and self.price < 0:
            raise ValueError("policy.price must be non-negative")
        if self.checkin < 0:
            raise ValueError("policy.checkin must be non-negative")
        if self.patrol_interval < 0:
            raise ValueError("policy.patrol_interval must be non-negative")
        if self.patrol_grace < 0:
            raise ValueError("policy.patrol_grace must be non-negative")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @property
    def is_priced(self) -> bool:
        return self.price is not None


PRESET_POLICIES: dict[str, Policy] = {
    "fcfs": Policy(key="fcfs", name="现状：先到先得"),
    "t30": Policy(key="t30", name="离座 30 分钟自动释放", release=30),
    "t15": Policy(key="t15", name="离座 15 分钟自动释放", release=15),
    "smart": Policy(
        key="smart",
        name="智能释放：占用>90% 才计时 30 分钟",
        release=30,
        trigger=0.90,
    ),
    "patrol": Policy(
        key="patrol",
        name="管理员每 45 分钟巡场清座",
        staff_patrol=True,
        patrol_interval=45,
        patrol_grace=10,
    ),
    "res": Policy(
        key="res",
        name="全预约 + 15 分钟到馆",
        reserve_share=1.0,
        planner_share=0.85,
    ),
    "mix": Policy(
        key="mix",
        name="30% 预约区 + 智能释放",
        reserve_share=0.30,
        planner_share=0.30,
        release=30,
        trigger=0.90,
    ),
    "credit": Policy(
        key="credit",
        name="30 分钟释放 + 信用分",
        release=30,
        credit=True,
    ),
    "price": Policy(key="price", name="计时收费 2 元/小时", price=2.0),
}

POLICY_DESCRIPTIONS: dict[str, str] = {
    "fcfs": "Baseline first-come-first-served. Seats are only released when the holder leaves.",
    "t30": "Reclaim a seat after 30 minutes of unattended time, all day.",
    "t15": "Reclaim a seat after 15 minutes of unattended time, all day.",
    "smart": "Only arm the 30-minute timer once occupancy exceeds 90%; silent when the library is calm.",
    "patrol": "A staff member walks the floor every 45 minutes and clears seats left empty for 10+ minutes.",
    "res": "Every seat is bookable 40 minutes ahead; unclaimed bookings expire 15 minutes after arrival.",
    "mix": "30% bookable seats + smart release on the rest.",
    "credit": "30-minute release with a violation score that lowers hoarding propensity.",
    "price": "Charge 2 CNY/hour; students below their personal value threshold do not enter.",
}

PRESETS: dict[str, dict] = {
    "calm": {"params": {"mean_present_h": 4.2}, "demand_scale": 1.0},
    "exam": {"params": {"mean_present_h": 4.9}, "demand_scale": 1.4},
}


def presets() -> dict[str, dict]:
    """Return the two calibrated demand regimes (calm term / exam week)."""
    return {
        k: {"params": dict(v["params"]), "demand_scale": v["demand_scale"]}
        for k, v in PRESETS.items()
    }


def resolve_policy(key: str | Policy, **overrides) -> Policy:
    """Look up a preset mechanism by key, optionally overriding fields."""
    if isinstance(key, Policy):
        return replace(key, **overrides) if overrides else key
    if key not in PRESET_POLICIES:
        raise ValueError(f"unknown policy {key!r}; expected one of {sorted(PRESET_POLICIES)}")
    return replace(PRESET_POLICIES[key], **overrides) if overrides else PRESET_POLICIES[key]
