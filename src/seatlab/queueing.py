"""Queueing-theory reference implementations used to validate the simulator.

Two independent things live here:

* ``erlang_c`` / ``mmc_wait`` — closed-form M/M/c waiting time.
* ``simulate_mmc`` — a *separate* discrete-event M/M/c simulator, written as
  a second opinion. Agreement between the two is the engine's external check.
"""

from __future__ import annotations

import heapq
import math
import random

from .rng import poisson


def erlang_c(c: int, offered: float) -> float:
    """Erlang-C probability that an arrival has to wait (log-domain form).

    The textbook formula fattens into ``c!`` overflow for a few hundred
    servers; everything here is evaluated in log space with a log-sum-exp,
    which stays exact up to thousands of servers.
    """
    if c <= 0:
        raise ValueError("c must be positive")
    if offered <= 0:
        return 0.0
    if offered >= c:
        return 1.0
    log_offered = math.log(offered)
    logs = [k * log_offered - math.lgamma(k + 1) for k in range(c)]
    tail = c * log_offered - math.lgamma(c + 1) + math.log(c / (c - offered))
    top = max(max(logs), tail)
    denom = sum(math.exp(x - top) for x in logs) + math.exp(tail - top)
    return math.exp(tail - top) / denom


def mmc_wait(c: int, lam: float, mu: float) -> float:
    """Mean queue waiting time in **minutes**.

    ``lam`` and ``mu`` are per minute, so Erlang-C already returns minutes and
    no unit conversion is needed.
    """
    if lam <= 0:
        return 0.0
    if lam >= c * mu:
        return float("inf")
    return erlang_c(c, lam / mu) / (c * mu - lam)


def simulate_mmc(c: int, lam: float, mu: float, n: int = 80_000, seed: int = 7) -> float:
    """Independent M/M/c event simulation; returns mean wait in minutes.

    ``n`` customers are generated at rate ``lam`` (per minute) and served at
    rate ``mu`` per server. The statistic is the mean wait over *all*
    customers (immediate service counts as a zero), which is what Erlang-C
    predicts. This deliberately shares no code path with the seat engine.
    """
    if c <= 0:
        raise ValueError("c must be positive")
    if lam <= 0 or mu <= 0:
        raise ValueError("rates must be positive")
    if lam >= c * mu:
        return float("inf")

    rng = random.Random(seed)
    clock = 0.0
    free = list(range(c))
    busy: list[tuple[float, int]] = []  # (service_end, server_id)
    waiting: list[float] = []  # arrival epochs, FIFO
    total_wait = 0.0

    for _ in range(n):
        clock += rng.expovariate(lam)
        while busy and busy[0][0] <= clock:
            end, sid = heapq.heappop(busy)
            if waiting:
                total_wait += end - waiting.pop(0)
                heapq.heappush(busy, (end + rng.expovariate(mu), sid))
            else:
                free.append(sid)
        if free:
            heapq.heappush(busy, (clock + rng.expovariate(mu), free.pop()))
        else:
            waiting.append(clock)

    # Drain the system so customers still queueing are counted too.
    while waiting:
        end, sid = heapq.heappop(busy)
        start = waiting.pop(0)
        begin = max(end, start)
        finish = begin + rng.expovariate(mu)
        total_wait += begin - start
        heapq.heappush(busy, (finish, sid))

    return total_wait / n


def poisson_arrivals(lam_per_minute: float, minutes: int, rng: random.Random) -> int:
    """Convenience wrapper: expected arrivals in ``minutes`` at ``lam``."""
    return poisson(rng, lam_per_minute * minutes)
