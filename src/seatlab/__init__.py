"""SeatLab — discrete-event seat allocation simulator and policy API.

Public surface
-------------
``Params`` / ``Policy``   model inputs (frozen dataclasses)
``simulate_day``          one day of micro-simulation, returns raw counters
``run``                   multi-day averaged metrics (the unit used by the API)
``compare``               run several mechanisms on the *same* random stream
``optimize``              grid search over release threshold × occupancy trigger
``pricing_curve``         welfare / equity trade-off along an hourly price
``scale_up``              extrapolate a library-level result to a national ledger

The engine is pure standard library: ``numpy`` and ``fastapi`` are optional.
"""

from __future__ import annotations

from .engine import build_students, gini, run, simulate_day
from .model import Params, Policy, presets, resolve_policy
from .queueing import erlang_c, mmc_wait, simulate_mmc
from .study import compare, optimize, pricing_curve, run_study, scale_up

__all__ = [
    "Params",
    "Policy",
    "build_students",
    "compare",
    "erlang_c",
    "gini",
    "mmc_wait",
    "optimize",
    "presets",
    "pricing_curve",
    "resolve_policy",
    "run",
    "run_study",
    "scale_up",
    "simulate_day",
    "simulate_mmc",
]

__version__ = "1.4.0"
