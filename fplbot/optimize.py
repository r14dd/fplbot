"""Squad selection and transfer planning.

There is no ILP solver in the environment, so squad choice is:

  1. seed with a sweep of price penalties (a Lagrangian relaxation of the
     budget constraint), which spans "all premiums" through "all value";
  2. hill-climb each promising seed over single AND paired swaps.

Paired swaps are the important part. The budget couples every position, so
escaping a local optimum usually needs one downgrade to fund one upgrade
somewhere else. No sequence of single swaps can find that move, because each
half of it is individually worse than staying put.

Everything on the hot path works against a precomputed {player_id: points}
dict rather than recomputing projections, and the squad evaluator avoids
allocating objects. That is the difference between this running in about a
second and taking several minutes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .model import POS_NAME, Model, Player

SQUAD_QUOTA = {1: 2, 2: 5, 3: 5, 4: 3}
MAX_PER_CLUB = 3
BUDGET = 100.0
BENCH_WEIGHT = 0.12  # bench points are worth something, but not much
HIT_COST = 4.0

# XI shape limits, per the API's element_types.
POS_MIN = {2: 3, 3: 2, 4: 1}
POS_MAX = {2: 5, 3: 5, 4: 3}


@dataclass
class Lineup:
    xi: list[Player]
    bench: list[Player]
    captain: Player
    vice: Player
    points: float

    @property
    def formation(self) -> str:
        c = Counter(p.pos for p in self.xi)
        return f"{c[2]}-{c[3]}-{c[4]}"


def valid_formations() -> list[tuple[int, int, int]]:
    out = []
    for d in range(POS_MIN[2], POS_MAX[2] + 1):
        for m in range(POS_MIN[3], POS_MAX[3] + 1):
            f = 10 - d - m
            if POS_MIN[4] <= f <= POS_MAX[4]:
                out.append((d, m, f))
    return out


FORMATIONS = valid_formations()


# ---------------------------------------------------------------- evaluation


def _value(squad: list[Player], sc: dict[int, float]) -> float:
    """Weighted squad value: best legal XI, plus a small credit for the bench.

    Hot path — no object construction, no sorting of Player instances.
    """
    gk: list[float] = []
    d: list[float] = []
    m: list[float] = []
    f: list[float] = []
    total = 0.0
    for p in squad:
        v = sc[p.id]
        total += v
        pos = p.pos
        if pos == 2:
            d.append(v)
        elif pos == 3:
            m.append(v)
        elif pos == 4:
            f.append(v)
        else:
            gk.append(v)
    if not gk:
        return -1e9

    d.sort(reverse=True)
    m.sort(reverse=True)
    f.sort(reverse=True)
    best_gk = max(gk)

    pd, pm, pf = [0.0], [0.0], [0.0]
    for v in d:
        pd.append(pd[-1] + v)
    for v in m:
        pm.append(pm[-1] + v)
    for v in f:
        pf.append(pf[-1] + v)

    best = -1e9
    for nd, nm, nf in FORMATIONS:
        if nd >= len(pd) or nm >= len(pm) or nf >= len(pf):
            continue
        v = best_gk + pd[nd] + pm[nm] + pf[nf]
        if v > best:
            best = v
    if best < -1e8:
        return -1e9

    # The captain scores double, so owning one elite ceiling is worth real
    # points and the objective has to say so — otherwise the optimiser sells
    # the best captaincy asset to spread the money around.
    # Every legal formation plays at least 3 DEF, 2 MID and 1 FWD, so the top
    # scorer in each position is always in the XI whichever shape is chosen.
    cap = best_gk
    if d and d[0] > cap:
        cap = d[0]
    if m and m[0] > cap:
        cap = m[0]
    if f and f[0] > cap:
        cap = f[0]

    return best + cap + BENCH_WEIGHT * (total - best)


def best_xi(squad: list[Player], score) -> Lineup:
    """Pick the highest-scoring legal XI from a 15-man squad."""
    by_pos: dict[int, list[Player]] = {pos: [] for pos in POS_NAME}
    for p in squad:
        by_pos[p.pos].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=score, reverse=True)

    if not by_pos[1]:
        raise ValueError("squad has no goalkeeper")

    best, best_pts = None, -1e18
    for d, m, f in FORMATIONS:
        if len(by_pos[2]) < d or len(by_pos[3]) < m or len(by_pos[4]) < f:
            continue
        xi = by_pos[1][:1] + by_pos[2][:d] + by_pos[3][:m] + by_pos[4][:f]
        pts = sum(score(p) for p in xi)
        if pts > best_pts:
            best, best_pts = xi, pts

    if best is None:
        raise ValueError("no legal formation from this squad")

    chosen = {p.id for p in best}
    # Bench order matters: autosubs only fire for a starter who played zero
    # minutes, and substitutes are tried in the order listed.
    bench_out = sorted(
        (p for p in squad if p.id not in chosen and p.pos != 1),
        key=score,
        reverse=True,
    )
    bench_gk = [p for p in squad if p.id not in chosen and p.pos == 1]
    ranked = sorted(best, key=score, reverse=True)
    return Lineup(
        xi=best,
        bench=bench_gk + bench_out,
        captain=ranked[0],
        vice=ranked[1] if len(ranked) > 1 else ranked[0],
        points=best_pts,
    )


def squad_score(squad: list[Player], score) -> float:
    """Public wrapper around `_value` for callers holding a score callable."""
    return _value(squad, {p.id: score(p) for p in squad})


def _legal(squad: list[Player], budget: float) -> bool:
    if len(squad) != 15:
        return False
    if Counter(p.pos for p in squad) != Counter(SQUAD_QUOTA):
        return False
    if max(Counter(p.team for p in squad).values()) > MAX_PER_CLUB:
        return False
    return sum(p.price for p in squad) <= budget + 1e-9


# ------------------------------------------------------------- construction


def _greedy(
    pool: list[Player],
    key,
    budget: float,
    locked: list[Player],
    cheapest: dict[int, float],
) -> list[Player] | None:
    """Fill 15 slots best-first under `key`, always reserving enough cash to
    legally complete the squad."""
    squad = list(locked)
    need = dict(SQUAD_QUOTA)
    for p in squad:
        need[p.pos] -= 1
        if need[p.pos] < 0:
            return None
    clubs = Counter(p.team for p in squad)
    spend = sum(p.price for p in squad)
    chosen = {p.id for p in squad}

    for p in sorted(pool, key=key, reverse=True):
        if len(squad) == 15:
            break
        if p.id in chosen or need.get(p.pos, 0) <= 0:
            continue
        if clubs[p.team] >= MAX_PER_CLUB:
            continue
        reserve = sum(
            cheapest[pos] * (n - (1 if pos == p.pos else 0))
            for pos, n in need.items()
            if n - (1 if pos == p.pos else 0) > 0
        )
        if spend + p.price + reserve > budget + 1e-9:
            continue
        squad.append(p)
        chosen.add(p.id)
        need[p.pos] -= 1
        clubs[p.team] += 1
        spend += p.price

    return squad if len(squad) == 15 else None


def _local_search(
    squad: list[Player],
    pool: list[Player],
    sc: dict[int, float],
    budget: float,
    locked_ids: set[int],
    max_passes: int = 25,
) -> list[Player]:
    by_pos: dict[int, list[Player]] = {}
    for p in pool:
        by_pos.setdefault(p.pos, []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: sc[p.id], reverse=True)

    # Pair search is quadratic, so restrict it to plausible candidates: the
    # best projections plus the cheapest fodder, which is what frees up cash.
    pair_pool = {
        pos: cands[:18] + sorted(cands, key=lambda p: p.price)[:8]
        for pos, cands in by_pos.items()
    }

    current = _value(squad, sc)

    for _ in range(max_passes):
        best_delta, best_move = 1e-9, None
        ids = {p.id for p in squad}
        total_spend = sum(p.price for p in squad)

        for i in range(15):
            out_p = squad[i]
            if out_p.id in locked_ids:
                continue
            rest = squad[:i] + squad[i + 1 :]
            rest_spend = total_spend - out_p.price
            rest_clubs = Counter(q.team for q in rest)
            for in_p in by_pos.get(out_p.pos, ()):
                if in_p.id in ids:
                    continue
                if rest_spend + in_p.price > budget + 1e-9:
                    continue
                if rest_clubs[in_p.team] >= MAX_PER_CLUB:
                    continue
                d = _value(rest + [in_p], sc) - current
                if d > best_delta:
                    best_delta, best_move = d, ("one", i, in_p, None, None)

        if best_move is None:
            for i in range(15):
                if squad[i].id in locked_ids:
                    continue
                for j in range(i + 1, 15):
                    if squad[j].id in locked_ids:
                        continue
                    rest = [q for k, q in enumerate(squad) if k != i and k != j]
                    rest_spend = total_spend - squad[i].price - squad[j].price
                    rest_clubs = Counter(q.team for q in rest)
                    cap = budget - rest_spend + 1e-9
                    for a in pair_pool.get(squad[i].pos, ()):
                        if a.id in ids or a.price > cap:
                            continue
                        if rest_clubs[a.team] >= MAX_PER_CLUB:
                            continue
                        room = cap - a.price
                        for b in pair_pool.get(squad[j].pos, ()):
                            if b.id in ids or b.id == a.id or b.price > room:
                                continue
                            if rest_clubs[b.team] + (1 if b.team == a.team else 0) >= MAX_PER_CLUB:
                                continue
                            d = _value(rest + [a, b], sc) - current
                            if d > best_delta:
                                best_delta, best_move = d, ("two", i, a, j, b)

        if best_move is None:
            break
        kind, i, a, j, b = best_move
        if kind == "one":
            squad = squad[:i] + squad[i + 1 :] + [a]
        else:
            squad = [q for k, q in enumerate(squad) if k != i and k != j] + [a, b]
        current += best_delta

    return squad


def pick_squad(
    model: Model,
    gws: list[int],
    budget: float = BUDGET,
    exclude: set[int] | None = None,
    locked: list[Player] | None = None,
    seeds_to_climb: int = 5,
) -> list[Player]:
    """Build a 15-man squad maximising weighted expected points."""
    exclude = exclude or set()
    locked = locked or []
    locked_ids = {p.id for p in locked}

    sc = {p.id: model.horizon(p, gws) for p in model.players}

    pool = [
        p
        for p in model.players
        if p.id not in exclude and sc[p.id] > 0
    ]
    have = {p.id for p in pool}
    pool.extend(p for p in locked if p.id not in have)

    cheapest = {
        pos: min((p.price for p in pool if p.pos == pos), default=99.0)
        for pos in SQUAD_QUOTA
    }

    seeds: list[tuple[float, list[Player]]] = []
    seen: set[frozenset] = set()
    for step in range(41):
        lam = step * 0.25
        seed = _greedy(
            pool, lambda p: sc[p.id] - lam * p.price, budget, locked, cheapest
        )
        if not seed or not _legal(seed, budget):
            continue
        key = frozenset(p.id for p in seed)
        if key in seen:
            continue
        seen.add(key)
        seeds.append((_value(seed, sc), seed))

    if not seeds:
        raise ValueError("could not build any legal squad within budget")

    seeds.sort(key=lambda t: t[0], reverse=True)
    best_squad, best_val = None, -1e18
    for _, seed in seeds[:seeds_to_climb]:
        cand = _local_search(seed, pool, sc, budget, locked_ids)
        val = _value(cand, sc)
        if val > best_val:
            best_squad, best_val = cand, val

    return best_squad


# --------------------------------------------------------------- transfers


def suggest_transfers(
    model: Model,
    squad: list[Player],
    gws: list[int],
    bank: float = 0.0,
    free_transfers: int = 1,
    sell_prices: dict[int, float] | None = None,
    top_n: int = 8,
) -> list[dict]:
    """Rank single transfers by net expected gain over the horizon."""
    sell_prices = sell_prices or {}
    sc = {p.id: model.horizon(p, gws) for p in model.players}

    base = _value(squad, sc)
    in_ids = {p.id for p in squad}
    pool = [p for p in model.players if p.id not in in_ids and sc[p.id] > 0]
    total_spend = sum(p.price for p in squad)

    out: list[dict] = []
    for i in range(len(squad)):
        out_p = squad[i]
        rest = squad[:i] + squad[i + 1 :]
        rest_clubs = Counter(q.team for q in rest)
        cash = bank + sell_prices.get(out_p.id, out_p.price)
        for in_p in pool:
            if in_p.pos != out_p.pos or in_p.price > cash + 1e-9:
                continue
            if rest_clubs[in_p.team] >= MAX_PER_CLUB:
                continue
            gain = _value(rest + [in_p], sc) - base
            out.append(
                {
                    "out": out_p,
                    "in": in_p,
                    "gain": gain,
                    "net": gain - (0.0 if free_transfers >= 1 else HIT_COST),
                    "bank_after": cash - in_p.price,
                }
            )
    out.sort(key=lambda d: d["net"], reverse=True)
    return out[:top_n]
