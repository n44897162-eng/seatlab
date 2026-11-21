"""The simulation engine.

One *day* is a minute-by-minute discrete-event loop over a fixed seat array.
A *run* repeats that day and averages the counters; all mechanisms share the
same student population and the same random stream, so comparisons are paired.

The code is deliberately explicit: no event framework, no heap, no numpy.
At 500 seats × 840 minutes a day costs a few milliseconds, which keeps a
40-day eight-policy comparison under a second — and makes the invariants
(seat counts, no double booking) testable line by line.
"""

from __future__ import annotations

import math
import random
from typing import Any

from .constants import BOOKED, FREE, HELD, HOUR_WEIGHTS, PRESENT
from .model import Params, Policy
from .rng import poisson


# --------------------------------------------------------------------------- #
# Population
# --------------------------------------------------------------------------- #
def _arrival_times(rng: random.Random, n: int, open_minutes: int) -> list[int]:
    """Draw arrival offsets in minutes from 08:00, shaped by HOUR_WEIGHTS.

    Hours are truncated to the opening window and re-weighted, so a shorter
    day keeps the same intra-day shape instead of piling arrivals up at the
    closing minute. With the default 840-minute day this is a no-op.
    """
    weights = []
    for index, weight in enumerate(HOUR_WEIGHTS):
        hour_start = index * 60
        covered = max(0, min(hour_start + 60, open_minutes) - hour_start)
        if covered:
            weights.append(weight * covered / 60.0)
    if not weights:
        return [0] * n

    cumulative, acc = [], 0.0
    for weight in weights:
        acc += weight
        cumulative.append(acc)
    total = cumulative[-1]

    times: list[int] = []
    for _ in range(n):
        u = rng.random() * total
        hi = 0
        while hi < len(cumulative) - 1 and cumulative[hi] < u:
            hi += 1
        start = hi * 60
        span = min(60, open_minutes - start)
        times.append(int(start + rng.random() * span))
    return times


def build_students(
    p: Params, rng: random.Random, demand_scale: float = 1.0
) -> list[dict[str, Any]]:
    """Create one day's population, drawing every random variate up front.

    Pre-drawing matters: the day loop can then be replayed under a different
    policy without consuming a different random stream.
    """
    n = int(round(p.cap * p.visits_per_seat * demand_scale))
    n = max(1, int(round(n * rng.gauss(1.0, p.daily_demand_jitter))))
    arrivals = _arrival_times(rng, n, p.open_minutes)

    students: list[dict[str, Any]] = []
    for sid, arrive in enumerate(arrivals):
        present = rng.lognormvariate(math.log(p.mean_present_h * 60), p.sigma_present)
        present = min(max(present, 35.0), 9.0 * 60)

        episodes: list[list[Any]] = []
        for idx, (lo, hi) in enumerate(
            [(11 * 60 + 20, 12 * 60 + 45), (17 * 60 + 10, 18 * 60 + 40)]
        ):
            if idx >= poisson(rng, p.n_away):
                continue
            offset = rng.uniform(lo, hi) - arrive
            duration = min(
                max(rng.lognormvariate(math.log(p.mean_away_min), p.sigma_away), 12.0), 150.0
            )
            episodes.append([max(0.0, offset), duration, False])

        for _ in range(poisson(rng, p.n_short)):
            episodes.append([rng.uniform(0.05, 0.95) * present, rng.uniform(6.0, 18.0), False])

        # Leaving for a lecture: the longest, most "legitimate" absence.
        for _ in range(poisson(rng, p.n_class)):
            episodes.append([rng.uniform(0.10, 0.80) * present, rng.uniform(55.0, 115.0), False])

        episodes.sort(key=lambda e: e[0])
        keep = [e for e in episodes if 8.0 < e[0] < present * 0.92]
        if sum(e[1] for e in keep) > 0.7 * present:
            keep = []
        for e in keep:
            e.append(rng.random())  # does this particular absence keep the seat?
        u_rejoin = [rng.random() for _ in keep]

        hoard = rng.betavariate(p.hoard_mu, p.hoard_kappa)
        value = rng.lognormvariate(math.log(p.value_median), p.value_sigma)

        students.append(
            {
                "sid": sid,
                "arr": arrive,
                "present": present,
                "present_left": present,
                "away": keep,
                "ai": 0,
                "hoard_p": hoard,
                "value": value,
                "state": "new",
                "seat": -1,
                "violations": 0,
                "wait": 0.0,
                "wait_accum": 0.0,
                "queue_enter": 0,
                "queued": False,
                "start": None,
                "last_resume": None,
                "realized": 0.0,
                "seated_once": False,
                "planner": False,
                "ghost": False,
                "u_planner": rng.random(),
                "patience": rng.expovariate(1.0 / p.patience_min),
                "u_rejoin": u_rejoin,
            }
        )
    return students


# --------------------------------------------------------------------------- #
# One day
# --------------------------------------------------------------------------- #
def simulate_day(
    p: Params,
    pol: Policy,
    rng: random.Random,
    demand_scale: float = 1.0,
    audit: bool = False,
) -> dict[str, Any]:
    """Simulate one day and return raw counters.

    ``audit=True`` re-derives the seat counters from the seat array every
    minute and records any disagreement in ``audit_errors``. It is used by
    the test-suite, not on the API hot path.
    """
    cap = p.cap
    students = build_students(p, rng, demand_scale)

    status = [FREE] * cap
    holder = [-1] * cap
    held_since = [0] * cap
    n_reserved = int(round(cap * pol.reserve_share))
    is_reserved = [i < n_reserved for i in range(cap)]
    free_any = list(range(cap - 1, n_reserved - 1, -1))
    free_reserved = list(range(n_reserved - 1, -1, -1))

    present_cnt = held_cnt = booked_cnt = 0
    seat_min_present = seat_min_held = seat_min_booked = 0
    n_hours = p.open_minutes // 60 + 1
    hour_sum = [0] * n_hours
    hour_cnt = [0] * n_hours

    queue: list[tuple[int, int]] = []
    events: dict[int, list[tuple[str, int]]] = {}
    served = balked = displaced = 0
    n_queued = 0  # distinct students that ever had to queue
    wait_sum = 0.0  # queue minutes of students that were eventually seated
    wait_sum_all = 0.0  # includes students that gave up in the queue
    ghost_waste = 0.0
    patrol_events = 0
    audit_errors: list[str] = []

    def push(t: float, kind: str, sid: int) -> None:
        tt = int(t)
        if 0 <= tt < p.open_minutes:
            events.setdefault(tt, []).append((kind, sid))

    # ---- planned arrivals / bookings -------------------------------------- #
    for s in students:
        if pol.price is not None and s["value"] < pol.price:
            s["state"] = "no_go"
            continue
        s["planner"] = pol.reserve_share > 0 and s["u_planner"] < pol.planner_share
        if s["planner"]:
            push(max(0, s["arr"] - 40), "book", s["sid"])
        push(s["arr"], "arrive", s["sid"])

    if pol.reserve_share > 0:
        n_planner = sum(1 for s in students if s["planner"])
        for _ in range(int(n_planner * pol.no_show)):
            sid = len(students)
            arrive = _arrival_times(rng, 1, p.open_minutes)[0]
            students.append(
                {
                    "sid": sid,
                    "arr": arrive,
                    "present": 0.0,
                    "present_left": 0.0,
                    "away": [],
                    "ai": 0,
                    "hoard_p": 0.0,
                    "value": 0.0,
                    "state": "ghost",
                    "seat": -1,
                    "violations": 0,
                    "wait": 0.0,
                    "wait_accum": 0.0,
                    "queue_enter": 0,
                    "queued": False,
                    "start": None,
                    "last_resume": None,
                    "realized": 0.0,
                    "seated_once": False,
                    "planner": True,
                    "ghost": True,
                }
            )
            push(max(0, arrive - 40), "book", sid)

    # ---- seat helpers ------------------------------------------------------ #
    def pop_any() -> int:
        while free_any:
            i = free_any.pop()
            if status[i] == FREE:
                return i
        while free_reserved:
            i = free_reserved.pop()
            if status[i] == FREE:
                return i
        return -1

    def pop_reserved() -> int:
        while free_reserved:
            i = free_reserved.pop()
            if status[i] == FREE:
                return i
        return -1

    def release_seat(seat: int, now: int) -> None:
        nonlocal present_cnt, held_cnt, booked_cnt
        if status[seat] == PRESENT:
            present_cnt -= 1
        elif status[seat] == HELD:
            held_cnt -= 1
        elif status[seat] == BOOKED:
            booked_cnt -= 1
        status[seat] = FREE
        holder[seat] = -1
        (free_reserved if is_reserved[seat] else free_any).append(seat)

    def schedule_segment(s: dict, now: int) -> None:
        s["state"] = "present"
        s["last_resume"] = now
        while s["ai"] < len(s["away"]):
            offset, _duration, done = s["away"][s["ai"]][:3]
            if done:
                s["ai"] += 1
                continue
            away_at = s["start"] + offset
            if away_at <= now:
                away_at = now + 1
            if away_at - now < s["present_left"]:
                push(away_at, "away", s["sid"])
                return
            s["ai"] += 1
        push(now + s["present_left"], "depart", s["sid"])

    def seat_student(s: dict, seat: int, now: int, count_served: bool = True) -> None:
        nonlocal present_cnt, served
        status[seat] = PRESENT
        holder[seat] = s["sid"]
        s["seat"] = seat
        present_cnt += 1
        if count_served and not s["seated_once"]:
            s["seated_once"] = True
            served += 1
        if s["start"] is None:
            s["start"] = now
        else:
            shift = now - s["start"]
            if shift > 0:
                s["start"] = now
                for e in s["away"]:
                    e[0] = max(e[0] - shift, 0.0)
        schedule_segment(s, now)

    def enqueue(s: dict, now: int) -> None:
        """Put a student at the end of the FIFO queue and remember when."""
        nonlocal n_queued
        s["state"] = "waiting"
        s["queue_enter"] = now
        if not s["queued"]:
            s["queued"] = True
            n_queued += 1
        queue.append((s["sid"], now + s["patience"]))

    def fill_queue(now: int) -> None:
        while queue:
            sid, _deadline = queue[0]
            s = students[sid]
            if s["state"] != "waiting":
                queue.pop(0)
                continue
            seat = pop_any()
            if seat < 0:
                return
            queue.pop(0)
            s["wait_accum"] += now - s["queue_enter"]
            s["wait"] = s["wait_accum"]
            seat_student(s, seat, now)

    max_queue = 0

    # ---- minute loop ------------------------------------------------------- #
    for now in range(p.open_minutes):
        max_queue = max(max_queue, len(queue))
        seat_min_present += present_cnt
        seat_min_held += held_cnt
        seat_min_booked += booked_cnt
        hour_sum[now // 60] += present_cnt
        hour_cnt[now // 60] += 1

        for kind, sid in events.get(now, ()):
            s = students[sid]

            if kind == "book":
                seat = pop_reserved()
                if seat >= 0:
                    status[seat] = BOOKED
                    holder[seat] = sid
                    s["seat"] = seat
                    booked_cnt += 1
                    push(max(s["arr"] + pol.checkin, now + 1), "expire", sid)

            elif kind == "expire":
                seat = s["seat"]
                if seat >= 0 and status[seat] == BOOKED and holder[seat] == sid:
                    release_seat(seat, now)
                    s["seat"] = -1
                    if s["ghost"]:
                        ghost_waste += 1

            elif kind == "arrive":
                if s["state"] != "new":
                    continue
                seat = s["seat"]
                if seat >= 0 and status[seat] == BOOKED and holder[seat] == sid:
                    booked_cnt -= 1
                    seat_student(s, seat, now)
                    continue
                seat = pop_any()
                if seat >= 0:
                    s["wait"] = 0.0
                    seat_student(s, seat, now)
                else:
                    enqueue(s, now)

            elif kind == "away":
                if s["state"] != "present" or s["ai"] >= len(s["away"]):
                    continue
                _offset, duration, _done = s["away"][s["ai"]][:3]
                elapsed = now - (s["last_resume"] or s["start"])
                s["present_left"] = max(0.0, s["present_left"] - elapsed)
                s["away"][s["ai"]][2] = True
                s["ai"] += 1

                hoard = s["hoard_p"]
                if pol.credit:
                    hoard *= max(0.0, 1.0 - pol.credit_effect * min(s["violations"], 2))
                seat = s["seat"]
                episode = s["away"][s["ai"] - 1]
                u = episode[3] if len(episode) > 3 else rng.random()
                if seat >= 0 and u < hoard:
                    status[seat] = HELD
                    held_since[seat] = now
                    present_cnt -= 1
                    held_cnt += 1
                    s["state"] = "away_hold"
                    occupancy = (present_cnt + held_cnt) / cap
                    if pol.release is not None and occupancy >= pol.trigger:
                        push(now + pol.release, "chk", sid)
                else:
                    if seat >= 0:
                        release_seat(seat, now)
                    s["seat"] = -1
                    s["state"] = "away_free"
                push(now + duration, "return", sid)

            elif kind == "return":
                seat = s["seat"]
                if (
                    s["state"] == "away_hold"
                    and seat >= 0
                    and holder[seat] == sid
                    and status[seat] == HELD
                ):
                    status[seat] = PRESENT
                    held_cnt -= 1
                    present_cnt += 1
                    schedule_segment(s, now)
                else:
                    if s["state"] == "away_hold":
                        s["violations"] += 1
                    s["seat"] = -1
                    idx = max(0, s["ai"] - 1)
                    u_rejoin = s["u_rejoin"][idx] if idx < len(s["u_rejoin"]) else 0.5
                    if u_rejoin < p.rejoin_prob:
                        seat2 = pop_any()
                        if seat2 >= 0:
                            s["wait"] = s["wait_accum"]
                            seat_student(s, seat2, now, count_served=False)
                        else:
                            enqueue(s, now)
                    else:
                        s["state"] = "left"
                        s["wait"] = s["wait_accum"]

            elif kind == "chk":
                seat = s["seat"]
                if (
                    s["state"] == "away_hold"
                    and seat >= 0
                    and status[seat] == HELD
                    and now - held_since[seat] >= (pol.release or 10**9)
                ):
                    release_seat(seat, now)
                    s["seat"] = -1
                    s["state"] = "away_free"
                    displaced += 1
                    if pol.credit:
                        s["violations"] += 1

            elif kind == "depart":
                if s["state"] == "present" and s["seat"] >= 0:
                    elapsed = now - (s["last_resume"] or s["start"])
                    s["present_left"] = max(0.0, s["present_left"] - elapsed)
                    release_seat(s["seat"], now)
                    s["seat"] = -1
                s["state"] = "done"

        # ---- staff patrol ---------------------------------------------- #
        if pol.staff_patrol and pol.patrol_interval and now % pol.patrol_interval == 0:
            patrol_events += 1
            for seat in range(cap):
                if status[seat] == HELD and now - held_since[seat] >= pol.patrol_grace:
                    owner = holder[seat]
                    release_seat(seat, now)
                    displaced += 1
                    if owner >= 0:
                        walker = students[owner]
                        walker["seat"] = -1
                        walker["state"] = "away_free"
                        walker["violations"] += 1

        fill_queue(now)

        if queue:
            keep_queue = []
            for sid, deadline in queue:
                s = students[sid]
                if s["state"] != "waiting":
                    continue
                if deadline <= now:
                    s["wait_accum"] += now - s["queue_enter"]
                    s["wait"] = s["wait_accum"]
                    s["state"] = "left"
                    balked += 1
                else:
                    keep_queue.append((sid, deadline))
            queue = keep_queue

        if audit:
            counts = {state: 0 for state in (FREE, PRESENT, HELD, BOOKED)}
            for state in status:
                counts[state] += 1
            if (
                counts[PRESENT] != present_cnt
                or counts[HELD] != held_cnt
                or counts[BOOKED] != booked_cnt
            ):
                audit_errors.append(
                    f"t={now}: counters {present_cnt}/{held_cnt}/{booked_cnt} "
                    f"!= array {counts[PRESENT]}/{counts[HELD]}/{counts[BOOKED]}"
                )
            if present_cnt + held_cnt + booked_cnt > cap:
                audit_errors.append(f"t={now}: over capacity")

    # ---- queue accounting ------------------------------------------------- #
    # A student who queues twice must be counted once: accumulate per student,
    # then sum. Anyone still waiting at closing time is charged up to closing.
    for s in students:
        if s["state"] == "waiting":
            s["wait_accum"] += p.open_minutes - s["queue_enter"]
            s["wait"] = s["wait_accum"]
    wait_sum = sum(s["wait_accum"] for s in students if s["seated_once"])
    wait_sum_all = sum(s["wait_accum"] for s in students if s["queued"])

    # ---- realised study time ---------------------------------------------- #
    for s in students:
        if s["state"] == "present" and s["last_resume"] is not None:
            s["realized"] = s["present"] - max(
                0.0, s["present_left"] - (p.open_minutes - s["last_resume"])
            )
        elif s["state"] in ("done", "away_hold", "away_free", "left"):
            s["realized"] = s["present"] - s["present_left"]
        else:
            s["realized"] = 0.0
        s["realized"] = max(0.0, min(s["realized"], s["present"]))

    real = [s for s in students if not s["ghost"] and s["state"] != "no_go"]
    first_fail = sum(1 for s in real if not s["seated_once"])
    n_buckets = 7
    bucket_arrivals = [0] * n_buckets
    bucket_fails = [0] * n_buckets
    for s in real:
        k = min(n_buckets - 1, s["arr"] // 120)
        bucket_arrivals[k] += 1
        if not s["seated_once"]:
            bucket_fails[k] += 1

    value_study = sum(s["realized"] / 60.0 * s["value"] for s in real)
    value_study += sum(
        s["present"] / 60.0 * s["value"] * p.alt_value_frac
        for s in students
        if s["state"] == "no_go"
    )
    value_wait = sum(s["wait"] / 60.0 * s["value"] for s in real)

    if audit:
        free_now = sum(1 for st in status if st == FREE)
        listed = len([i for i in free_any if status[i] == FREE]) + len(
            [i for i in free_reserved if status[i] == FREE]
        )
        if free_now != listed:
            audit_errors.append(f"end: free seats {free_now} != freelist {listed}")
        if len(free_any) != len(set(free_any)):
            audit_errors.append("end: duplicate seat ids in free list")

    return {
        "cap": cap,
        "arrivals": len(real),
        "served": served,
        "balked": balked,
        "first_fail": first_fail,
        "wait_sum": wait_sum,
        "wait_sum_all": wait_sum_all,
        "waits": [s["wait_accum"] for s in real if s["seated_once"]],
        "n_queued": n_queued,
        "wait_per_served": wait_sum / max(1, served),
        "wait_mean_queued": wait_sum_all / max(1, n_queued),
        "present_h": seat_min_present / 60.0,
        "held_h": seat_min_held / 60.0,
        "booked_h": seat_min_booked / 60.0,
        "avail_h": cap * p.open_minutes / 60.0,
        "util": seat_min_present / (cap * p.open_minutes),
        "empty_share": seat_min_held / max(1e-9, seat_min_present + seat_min_held),
        "displaced": displaced,
        "patrol_events": patrol_events,
        "ghost_waste": ghost_waste,
        "util_profile": [
            hour_sum[i] / (cap * hour_cnt[i]) if hour_cnt[i] else 0.0 for i in range(n_hours)
        ],
        "peak_util": max(
            hour_sum[i] / (cap * hour_cnt[i]) if hour_cnt[i] else 0.0 for i in range(n_hours)
        ),
        "study": [s["realized"] / 60.0 for s in real],
        "values": [s["value"] for s in real],
        "bucket_arr": bucket_arrivals,
        "bucket_fail": bucket_fails,
        "value_study": value_study,
        "value_wait": value_wait,
        "no_go": sum(1 for s in students if s["state"] == "no_go"),
        "total_demand": sum(1 for s in students if not s["ghost"]),
        "max_queue": max_queue,
        "audit_errors": audit_errors,
    }


# --------------------------------------------------------------------------- #
# Multi-day aggregation
# --------------------------------------------------------------------------- #
def gini(xs: list[float]) -> float:
    """Gini coefficient of a non-negative distribution."""
    if not xs:
        return 0.0
    xs = sorted(xs)
    n = len(xs)
    total = sum(xs)
    if total <= 0:
        return 0.0
    return sum((2 * i - n - 1) * x for i, x in enumerate(xs, 1)) / (n * total)


def percentile(xs: list[float], q: float) -> float:
    """Linear-interpolated quantile; ``q`` in [0, 1]. Empty sample -> 0."""
    if not xs:
        return 0.0
    data = sorted(xs)
    if len(data) == 1:
        return float(data[0])
    pos = q * (len(data) - 1)
    low = int(math.floor(pos))
    high = min(len(data) - 1, low + 1)
    weight = pos - low
    return float(data[low] * (1 - weight) + data[high] * weight)


def run(
    p: Params,
    pol: Policy,
    demand_scale: float = 1.0,
    days: int | None = None,
) -> dict[str, Any]:
    """Run ``p.days`` days and average the counters into policy metrics."""
    days = days or p.days
    # One independent stream per day, derived up-front from the master seed.
    # Drawing them before any day runs keeps the demand realisation identical
    # across mechanisms (paired comparison) and makes days order-independent.
    master = random.Random(p.seed)
    day_seeds = [master.getrandbits(64) for _ in range(days)]
    acc = [simulate_day(p, pol, random.Random(s), demand_scale) for s in day_seeds]
    n = len(acc)

    def mean(key: str) -> float:
        return sum(a[key] for a in acc) / n

    study = [x for a in acc for x in a["study"]]
    values = [x for a in acc for x in a["values"]]
    waits = [x for a in acc for x in a["waits"]]
    v_bar = sum(values) / max(1, len(values))
    present_h, held_h = mean("present_h"), mean("held_h")
    arrivals, served = mean("arrivals"), mean("served")
    study_value = mean("value_study")
    wait_cost = mean("value_wait") * p.wait_disu
    monitor = p.monitor_cost_per_seat_day * p.cap * (1.35 if pol.staff_patrol else 1.0)
    revenue = (pol.price or 0.0) * present_h if pol.price is not None else 0.0

    return {
        "name": pol.name,
        "key": pol.key,
        "cap": p.cap,
        "demand_scale": demand_scale,
        "days": days,
        "arrivals": arrivals,
        "served": served,
        "balked": mean("balked"),
        "first_fail": mean("first_fail"),
        "served_rate": served / max(1.0, arrivals),
        "fail_rate": mean("first_fail") / max(1.0, arrivals),
        "util": mean("util"),
        "peak_util": mean("peak_util"),
        "util_profile": [
            sum(a["util_profile"][i] for a in acc) / n for i in range(len(acc[0]["util_profile"]))
        ],
        "present_h": present_h,
        "held_h": held_h,
        "booked_h": mean("booked_h"),
        "empty_share": mean("empty_share"),
        "wait": mean("wait_per_served"),
        "wait_mean_queued": sum(a["wait_sum_all"] for a in acc)
        / max(1.0, sum(a["n_queued"] for a in acc)),
        "queue_share": min(1.0, mean("n_queued") / max(1.0, arrivals)),
        "wait_p50": percentile(waits, 0.50),
        "wait_p90": percentile(waits, 0.90),
        "displaced": mean("displaced"),
        "patrol_events": mean("patrol_events"),
        "ghost_waste": mean("ghost_waste"),
        "avg_study_h": sum(study) / max(1, len(study)),
        "study_p10": percentile(study, 0.10),
        "study_p50": percentile(study, 0.50),
        "gini": gini(study),
        "no_go": mean("no_go"),
        "total_demand": mean("total_demand"),
        "fail_by_hour": [
            sum(a["bucket_fail"][i] for a in acc) / max(1, sum(a["bucket_arr"][i] for a in acc))
            for i in range(7)
        ],
        "arrivals_by_hour": [sum(a["bucket_arr"][i] for a in acc) / n for i in range(7)],
        "study_value_day": study_value,
        "wait_cost_day": wait_cost,
        "monitor_day": monitor,
        "welfare_day": study_value - wait_cost - monitor,
        "revenue_day": revenue,
        "v_bar": v_bar,
        "max_queue": mean("max_queue"),
    }
