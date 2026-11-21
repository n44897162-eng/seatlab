"""Model constants shared by the engine, the study driver and the API."""

from __future__ import annotations

OPEN_MINUTE = 8 * 60
DAY_MINUTES = 840

# Relative arrival intensity for each hour from 08:00 to 22:00.
# Sum is normalised at run time, so only the shape matters.
HOUR_WEIGHTS = (
    0.16,
    0.13,
    0.10,
    0.08,
    0.05,
    0.05,
    0.07,
    0.09,
    0.10,
    0.08,
    0.05,
    0.03,
    0.01,
    0.00,
)

# Seat states.
FREE, PRESENT, HELD, BOOKED = 0, 1, 2, 3

STATE_NAMES = {FREE: "free", PRESENT: "present", HELD: "held", BOOKED: "booked"}
