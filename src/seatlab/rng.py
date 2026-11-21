"""Small random-number helpers that the standard library does not provide."""

from __future__ import annotations

import math
import random


def poisson(rng: random.Random, lam: float) -> int:
    """Draw a Poisson variate with Knuth's product method.

    ``random.Random`` has no ``poisson`` method, and the model only needs
    small means (0.4 - 3), so the O(lam) product loop is the right trade-off.
    """
    if lam <= 0:
        return 0
    limit = math.exp(-lam)
    k, product = 0, 1.0
    while True:
        product *= rng.random()
        if product <= limit:
            return k
        k += 1


def beta_with_mean(mean: float, kappa: float) -> tuple[float, float]:
    """Convert a target mean and concentration into Beta(a, b) parameters."""
    mean = min(max(mean, 1e-4), 1 - 1e-4)
    return mean * kappa, (1 - mean) * kappa
