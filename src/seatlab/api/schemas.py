"""Pydantic v2 schemas for the HTTP API.

Field descriptions double as the Swagger documentation, so keep them in sync
with the model docstrings in :mod:`seatlab.model`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..model import Params, Policy, resolve_policy

PolicyKeyLiteral = Literal["fcfs", "t30", "t15", "smart", "patrol", "res", "mix", "credit", "price"]


class ParamsIn(BaseModel):
    """The library and its population. All durations in minutes, money in CNY."""

    model_config = ConfigDict(extra="forbid")

    cap: int = Field(500, ge=10, le=5000, description="Number of seats")
    visits_per_seat: float = Field(2.03, gt=0, le=10, description="Arrivals per seat per day")
    mean_present_h: float = Field(4.20, gt=0.5, lt=13, description="Mean length of stay, hours")
    sigma_present: float = Field(0.55, gt=0, le=2, description="Log-normal sigma of stay length")
    mean_away_min: float = Field(45.0, gt=0, le=240, description="Mean meal break, minutes")
    sigma_away: float = Field(0.50, gt=0, le=2, description="Log-normal sigma of meal break")
    n_away: float = Field(1.5, ge=0, le=4, description="Expected meal breaks per student")
    n_short: float = Field(1.2, ge=0, le=5, description="Expected short absences per student")
    n_class: float = Field(0.40, ge=0, le=5, description="Expected lecture absences (55-115 min)")
    hoard_mu: float = Field(2.0, gt=0, le=50, description="Beta α of hoarding propensity")
    hoard_kappa: float = Field(1.4, gt=0, le=50, description="Beta β of hoarding propensity")
    patience_min: float = Field(22.0, gt=0, le=240, description="Mean patience before giving up")
    rejoin_prob: float = Field(
        0.90, ge=0, le=1, description="Probability of coming back after losing a seat"
    )
    value_median: float = Field(
        2.60, gt=0, le=50, description="Median value of study time, CNY/hour"
    )
    value_sigma: float = Field(0.55, gt=0, le=2, description="Log-normal sigma of time value")
    wait_disu: float = Field(0.55, ge=0, le=5, description="Waiting-cost multiplier")
    monitor_cost_per_seat_day: float = Field(
        0.35, ge=0, le=20, description="Order-keeping cost per seat per day"
    )
    alt_value_frac: float = Field(
        0.45, ge=0, le=1, description="Value of the fallback study environment"
    )
    open_minutes: int = Field(
        840, ge=60, le=840, description="Opening window in minutes (08:00 + N)"
    )
    days: int = Field(30, ge=1, le=365, description="Days simulated per policy")
    seed: int = Field(20261121, ge=0, le=4_294_967_295, description="Random seed")
    daily_demand_jitter: float = Field(0.06, ge=0, le=0.5, description="Day-to-day demand noise")

    def to_params(self) -> Params:
        return Params(**self.model_dump())


class PolicyIn(BaseModel):
    """One allocation mechanism.

    Only the fields you actually send override the preset named by ``key``,
    so ``{"key": "smart"}`` keeps the tuned 30-minute / 90-percent defaults.
    """

    model_config = ConfigDict(extra="forbid")

    key: PolicyKeyLiteral = Field("fcfs", description="Preset mechanism")
    name: str | None = Field(None, max_length=80, description="Display name override")
    release: int | None = Field(
        None, ge=1, le=600, description="Reclaim after N unattended minutes"
    )
    trigger: float = Field(0.0, ge=0, le=1, description="Arm the timer only above this occupancy")
    reserve_share: float = Field(0.0, ge=0, le=1, description="Share of bookable seats")
    checkin: int = Field(15, ge=0, le=120, description="Bookings expire N minutes after arrival")
    no_show: float = Field(0.18, ge=0, le=1, description="Share of ghost bookings")
    planner_share: float = Field(0.0, ge=0, le=1, description="Share of students who plan ahead")
    credit: bool = Field(False, description="Enable the violation-score discount")
    credit_effect: float = Field(0.45, ge=0, le=1, description="Strength of the credit discount")
    price: float | None = Field(None, ge=0, le=50, description="Hourly price in CNY")
    staff_patrol: bool = Field(
        False, description="Enable floor patrols (real sweeps, not just cost)"
    )
    patrol_interval: int = Field(
        45, ge=0, le=240, description="Minutes between patrol sweeps (0 = off)"
    )
    patrol_grace: int = Field(
        10, ge=0, le=120, description="A seat must be empty this long to be cleared"
    )

    def to_policy(self) -> Policy:
        provided = {k: v for k, v in self.model_dump(exclude_unset=True).items() if k != "key"}
        return resolve_policy(self.key, **provided)


class MetricsOut(BaseModel):
    """Aggregated metrics for one mechanism over one scenario."""

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str
    cap: int
    demand_scale: float
    days: int

    arrivals: float = Field(description="Average arrivals per day")
    served: float = Field(description="Average students that got a seat at least once")
    served_rate: float = Field(description="served / arrivals")
    fail_rate: float = Field(description="First-visit failure rate")
    balked: float = Field(description="Average students that gave up queueing")

    util: float = Field(description="Seat-minutes occupied by a present student / capacity")
    peak_util: float = Field(description="Busiest hour utilisation")
    present_h: float = Field(description="Seat-hours actually studied, per day")
    held_h: float = Field(description="Seat-hours held with nobody present (the externality)")
    booked_h: float = Field(description="Seat-hours reserved but unclaimed")
    empty_share: float = Field(description="held / (present + held)")

    wait: float = Field(description="Queue minutes per served student")
    wait_p50: float = Field(default=0.0, description="Median queue minutes among served students")
    wait_p90: float = Field(
        default=0.0, description="90th percentile queue minutes among served students"
    )
    wait_mean_queued: float = Field(description="Mean wait among students who had to queue")
    queue_share: float = Field(description="Share of arrivals that had to queue")
    displaced: float = Field(description="Seats reclaimed from absent holders")
    patrol_events: float = Field(default=0.0, description="Patrol sweeps performed per day")
    ghost_waste: float = Field(description="Bookings that expired unused")

    avg_study_h: float
    study_p10: float = Field(
        default=0.0, description="10th percentile study hours (who gets squeezed)"
    )
    study_p50: float = Field(default=0.0, description="Median study hours")
    gini: float
    no_go: float = Field(description="Demand crowded out by price")
    total_demand: float

    welfare_day: float = Field(description="Study value - waiting cost - monitoring cost, CNY/day")
    revenue_day: float = Field(
        default=0.0, description="Ticket revenue, CNY/day (a transfer, not welfare)"
    )
    study_value_day: float
    wait_cost_day: float
    monitor_day: float

    fail_by_hour: list[float]
    arrivals_by_hour: list[float]
    util_profile: list[float]


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    policy: PolicyIn = Field(default_factory=PolicyIn)
    demand_scale: float = Field(1.0, ge=0.3, le=3.0, description="1.0 = calm term, 1.4 = exam week")


class SimulateOut(BaseModel):
    metrics: MetricsOut
    welfare_breakdown: dict[str, float]


class CompareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    policies: list[PolicyKeyLiteral] = Field(
        default_factory=lambda: [
            "fcfs",
            "t30",
            "t15",
            "smart",
            "patrol",
            "res",
            "mix",
            "credit",
            "price",
        ],
        min_length=1,
        max_length=9,
    )
    demand_scale: float = Field(1.0, ge=0.3, le=3.0)


class CompareOut(BaseModel):
    rows: list[MetricsOut]
    base_key: str
    best_welfare_key: str
    welfare_gain_pct: dict[str, float]


class OptimizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    demand_scale: float = Field(1.4, ge=0.3, le=3.0)
    days: int = Field(16, ge=1, le=120)
    release_values: list[int] = Field(
        default_factory=lambda: [10, 15, 20, 25, 30, 40, 50, 60], max_length=20
    )
    trigger_values: list[float] = Field(
        default_factory=lambda: [0.75, 0.85, 0.90, 0.95, 1.00], max_length=10
    )


class GridCell(BaseModel):
    release: int
    trigger: float
    served_rate: float
    welfare_day: float
    empty_share: float
    wait: float
    displaced: float
    avg_study_h: float


class OptimizeOut(BaseModel):
    rows: list[GridCell]
    best_welfare: GridCell
    best_service: GridCell
    demand_scale: float
    days: int


class PricingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    demand_scale: float = Field(1.4, ge=0.3, le=3.0)
    days: int = Field(16, ge=1, le=120)
    prices: list[float] = Field(
        default_factory=lambda: [0, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5], max_length=12
    )


class PricingRow(BaseModel):
    price: float
    welfare_day: float
    gini: float
    avg_study_h: float
    served_rate: float
    revenue_day: float
    crowd_out: float
    wait: float


class PricingOut(BaseModel):
    rows: list[PricingRow]
    best: PricingRow
    gain_vs_free_pct: float | None


class ScaleUpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    students_wan: float = Field(4301.0, gt=0, le=20_000, description="Students, in ten-thousands")
    universities: int = Field(3119, ge=1, le=50_000)
    seats_per_student: tuple[float, float] = Field(
        (0.10, 0.14), description="Low/high seats per student"
    )
    peak_days: int = Field(110, ge=0, le=365)
    calm_days: int = Field(170, ge=0, le=365)
    open_hours: float = Field(14.0, gt=0, le=24)
    empty_exam: float = Field(0.087, ge=0, le=1)
    empty_calm: float = Field(0.090, ge=0, le=1)
    value_per_hour: float = Field(2.6, gt=0, le=100)


class ScaleUpOut(BaseModel):
    students_wan: float
    universities: int
    seats_wan: list[float]
    empty_exam: float
    empty_calm: float
    wasted_hours_yi: list[float]
    yuan_yi: list[float]
    hours_per_student_year: list[float]
    assumptions: dict


class CalibrateTargets(BaseModel):
    """What the library can actually measure before changing anything."""

    model_config = ConfigDict(extra="forbid")

    arrivals_per_day: float | None = Field(None, gt=0, description="Observed gate count per day")
    empty_share: float | None = Field(
        None, ge=0, le=0.99, description="Observed share of occupied seat-time with nobody present"
    )
    mean_study_h: float | None = Field(
        None, gt=0, le=10, description="Observed hours studied per student"
    )


class CalibrateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    targets: CalibrateTargets
    demand_scale: float = Field(
        1.0, ge=0.3, le=3.0, description="Only used to scale the synthetic population"
    )
    days: int = Field(6, ge=1, le=60)
    hoard_kappa: float = Field(
        1.4, gt=0, le=50, description="Beta β held fixed while fitting the mean"
    )


class CalibrateOut(BaseModel):
    params: dict[str, float]
    achieved: dict[str, float]
    targets: dict[str, float | None]
    residuals: dict[str, float]
    saturated: list[str] = []
    trace: list[dict[str, float | str]]
    runs: int
    days: int
    demand_scale: float
    hoard_kappa: float


class SweepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: ParamsIn = Field(default_factory=ParamsIn)
    policy: PolicyKeyLiteral = Field("fcfs", description="Mechanism to sweep")
    caps: list[int] = Field(
        default_factory=lambda: [380, 420, 460, 500, 540, 580, 620], min_length=1, max_length=10
    )
    demands: list[float] = Field(
        default_factory=lambda: [1.0, 1.1, 1.2, 1.3, 1.4, 1.5], min_length=1, max_length=10
    )
    days: int = Field(8, ge=1, le=60)


class SweepCell(BaseModel):
    cap: int
    demand: float
    served_rate: float
    empty_share: float
    wait: float
    util: float
    peak_util: float
    welfare_day: float


class SweepOut(BaseModel):
    key: str
    name: str
    caps: list[int]
    demands: list[float]
    days: int
    rows: list[SweepCell]
    best: SweepCell


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    engine: str
    storage: str | None = None
    schema_version: int | None = None
    auth_mode: str | None = None
