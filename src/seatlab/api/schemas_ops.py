"""Request models for the control plane (admin) and the student app.

The simulation endpoints keep their own schema module; these models only cover
stateful operations, so they stay small and split ``extra="forbid"``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VenuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=80)
    cap: int | None = Field(None, ge=10, le=5000)
    open_minute: int | None = Field(None, ge=0, le=1439)
    close_minute: int | None = Field(None, ge=1, le=1440)
    zones: list[dict[str, Any]] | None = None
    reservation_grace_min: int | None = Field(None, ge=1, le=120)
    away_limit_min: int | None = Field(None, ge=5, le=240)
    max_active_reservations: int | None = Field(None, ge=1, le=5)
    max_reservation_minutes: int | None = Field(None, ge=30, le=720)
    credit_threshold: int | None = Field(None, ge=1, le=20)
    credit_block_days: int | None = Field(None, ge=0, le=60)
    credit_window_days: int | None = Field(None, ge=1, le=365)
    demand_scale: float | None = Field(None, gt=0, le=3)
    mean_present_h: float | None = Field(None, ge=0.5, le=13)
    seed: int | None = Field(None, ge=0, le=2**32 - 1)


class PolicyActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demands: list[float] = Field(
        default_factory=lambda: [1.0, 1.2, 1.4], min_length=1, max_length=10
    )
    days: int = Field(7, ge=1, le=60)
    caps: list[int] | None = Field(None, max_length=10)


class StudentOnly(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: str = Field("", max_length=40, description="optional if X-Student-ID is sent")


class ReserveIn(StudentOnly):
    seat_id: int | None = Field(None, ge=1)
    zone: str | None = Field(None, max_length=8)
    minutes: int | None = Field(None, ge=30, le=720)


class CheckinIn(StudentOnly):
    code: str = Field(..., min_length=4, max_length=4)


class AwayIn(StudentOnly):
    minutes: int = Field(15, ge=1, le=240)


class ReportIn(StudentOnly):
    seat_id: int = Field(..., ge=1)
    note: str = Field(..., min_length=1, max_length=500)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field("", max_length=500)


class WaiveIn(ResolveIn):
    pass


class AnnounceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=80)
    body: str = Field(..., min_length=1, max_length=2000)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    venue: dict[str, Any]


class WaitlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: str = Field("", max_length=40, description="optional if X-Student-ID is sent")
    zone: str = Field(..., min_length=1, max_length=8)


SeatStatus = Literal["free", "reserved", "occupied", "away"]
