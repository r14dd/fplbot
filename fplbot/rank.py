"""Rank simulation for a mini-league: who finishes first after a gameweek.

Every player's points are drawn once per simulation and applied to every
manager who owns him, so shared players cancel and differentials decide it.
Not modelled: within-team correlation, rival chips and transfers, autosubs.
"""

from __future__ import annotations

import math
import random
from collections import Counter

from . import api


def dispersion(finished: list[int]) -> float:
    """Var/mean of (points - 2) for players who played, over finished GWs."""
    xs = []
    for gw in finished:
        for e in api.event_live(gw)["elements"]:
            if e["stats"]["minutes"] > 0:
                xs.append(e["stats"]["total_points"] - 2)
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    return max(var / mean, 1.0)


def _poisson(lam: float, rng: random.Random) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _draw(p, gw: int, k: float, rng: random.Random) -> float:
    xp = p.xpts_by_gw.get(gw, 0.0)
    if xp <= 0:
        return 0.0
    p_play = min(1.0, p.availability * p.exp_mins / 80.0)
    if p_play <= 0 or rng.random() > p_play:
        return 0.0
    lam = max(xp / p_play - 2.0, 0.0)
    return 2.0 + k * _poisson(lam / k, rng)


def standings(league_id: int) -> list[dict]:
    res = api.league_standings(league_id)["standings"]["results"]
    return [{"entry": r["entry"], "name": r["player_name"], "total": r["total"]} for r in res]


def rival_picks(entry: int, gw: int) -> list[tuple[int, int]]:
    """(player_id, multiplier) for the XI, frozen on the current squad."""
    pk = api.entry_picks(entry, gw)["picks"]
    return [(x["element"], x["multiplier"]) for x in pk if x["position"] <= 11]


def simulate(model, gw: int, mgrs: list[dict], sims: int, k: float, seed: int = 7):
    """mgrs: [{name, total, picks:[(pid, mult)]}]. Returns rank Counters, mean GW score."""
    rng = random.Random(seed)
    by_id = model.by_id()
    pids = {pid for m in mgrs for pid, _ in m["picks"]}
    ranks = [Counter() for _ in mgrs]
    sums = [0.0] * len(mgrs)
    for _ in range(sims):
        pts = {pid: _draw(by_id[pid], gw, k, rng) for pid in pids}
        tot = []
        for i, m in enumerate(mgrs):
            gain = sum(pts[pid] * mult for pid, mult in m["picks"])
            sums[i] += gain
            tot.append(m["total"] + gain)
        order = sorted(range(len(mgrs)), key=lambda i: -tot[i])
        for r, i in enumerate(order, 1):
            ranks[i][r] += 1
    return ranks, [s / sims for s in sums]
