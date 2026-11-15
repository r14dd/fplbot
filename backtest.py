"""Backtest the projection model on past gameweeks with no lookahead.

For each target gameweek g, the bootstrap is rebuilt from player histories
using only rounds before g, and the model's xP is scored against what players
actually got in g. Two baselines run on the same players: season points per
appearance, and the mean of the last four games, each times the fixtures in g.

Injury news cannot be rebuilt, so every player is treated as available; the
fixture difficulty ratings are today's. Needs the element-summary cache (fetch
it once with a live run), then works offline:

    python3 backtest.py                       # report to stdout
    python3 backtest.py --tune SHRINK_90S=4,6,9 REGRESS_W=0.25,0.5,0.75
"""

from __future__ import annotations

import argparse
import itertools
import math
from collections import defaultdict

from fplbot import api
from fplbot import model as M
from fplbot.model import POS_NAME, Model

FIRST, LAST = 4, 10
FIT, HOLD = range(4, 8), range(8, 11)
ENTRY = 6412208
BANDS = [(0, 5.0, "<5.0"), (5.0, 7.0, "5.0-6.9"), (7.0, 9.0, "7.0-8.9"), (9.0, 99, "9.0+")]


def load():
    bs, fx = api.bootstrap(), api.fixtures()
    hist = {e["id"]: api.element_summary(e["id"])["history"] for e in bs["elements"]}
    return bs, fx, hist


def snapshot(bs: dict, hist: dict, g: int) -> dict:
    """The bootstrap as it stood before gameweek g's deadline."""
    elements = []
    for e in bs["elements"]:
        rows = [r for r in hist[e["id"]] if r["round"] < g]
        now = [r for r in hist[e["id"]] if r["round"] == g]
        priced = now or rows[-1:]
        elements.append(dict(
            e,
            minutes=sum(r["minutes"] for r in rows),
            starts=sum(r["starts"] for r in rows),
            total_points=sum(r["total_points"] for r in rows),
            goals_scored=sum(r["goals_scored"] for r in rows),
            assists=sum(r["assists"] for r in rows),
            expected_goals=str(sum(float(r["expected_goals"]) for r in rows)),
            expected_assists=str(sum(float(r["expected_assists"]) for r in rows)),
            now_cost=priced[0]["value"] if priced else e["now_cost"],
            status="a", chance_of_playing_next_round=None, news="", news_added=None,
        ))
    events = [dict(ev, finished=ev["id"] < g, is_current=ev["id"] == g - 1,
                   is_next=ev["id"] == g) for ev in bs["events"]]
    return dict(bs, elements=elements, events=events)


def self_check(bs: dict, hist: dict) -> list[str]:
    """Rebuilt totals after the last finished GW must equal the live ones."""
    g = sum(1 for e in bs["events"] if e["finished"]) + 1
    snap = {e["id"]: e for e in snapshot(bs, hist, g)["elements"]}
    bad = []
    for e in bs["elements"]:
        for k in ("minutes", "starts", "total_points", "goals_scored", "assists"):
            if snap[e["id"]][k] != e[k]:
                bad.append(f"{e['web_name']} {k}: rebuilt {snap[e['id']][k]} live {e[k]}")
    return bad


def rows_for(bs, fx, hist, g):
    """One row per player with minutes before g: predictions and the outcome."""
    m = Model(snapshot(bs, hist, g), fx)
    m.project([g])
    fixtures = m.team_fixtures(g)
    out = []
    for p in m.players:
        if p.minutes <= 0:
            continue
        past = [r for r in hist[p.id] if r["round"] < g]
        now = [r for r in hist[p.id] if r["round"] == g]
        n = len(fixtures.get(p.team, []))
        apps = sum(1 for r in past if r["minutes"] > 0)
        last4 = past[-4:]
        out.append({
            "id": p.id, "g": g, "pos": p.pos, "price": p.price,
            "total": p.total_points,
            "model": p.xpts_by_gw[g],
            "ppg": p.total_points / apps * n,
            "last4": sum(r["total_points"] for r in last4) / len(last4) * n,
            "actual": sum(r["total_points"] for r in now),
            "played": any(r["minutes"] > 0 for r in now),
        })
    return out


METHODS = ("model", "ppg", "last4")


def stats(rows, method):
    if not rows:
        return float("nan"), float("nan")
    err = [r[method] - r["actual"] for r in rows]
    return sum(err) / len(err), math.sqrt(sum(e * e for e in err) / len(err))


def table(title, groups):
    L = [f"### {title}", "", "| Group | n | " + " | ".join(
        f"{m} bias | {m} RMSE" for m in METHODS) + " |",
         "|---|---|" + "---|---|" * len(METHODS)]
    for name, rows in groups:
        cells = []
        for m in METHODS:
            b, r = stats(rows, m)
            cells += [f"{b:+.2f}", f"{r:.2f}"]
        L.append(f"| {name} | {len(rows)} | " + " | ".join(cells) + " |")
    return L + [""]


def captains(bs, fx, hist):
    by = {}
    for g in range(FIRST, LAST + 1):
        by.update({(r["id"], g): r for r in rows_for(bs, fx, hist, g)})
    names = {e["id"]: e["web_name"] for e in bs["elements"]}
    L = ["## Captain, on the squad actually held", "",
         "| GW | Model pick | pts | Top scorer pick | pts | Your pick | pts |",
         "|---|---|---|---|---|---|---|"]
    tot = defaultdict(int)
    for g in range(FIRST, LAST + 1):
        picks = api.entry_picks(ENTRY, g)["picks"]
        cands = [by[(p["element"], g)] for p in picks if (p["element"], g) in by]
        model = max(cands, key=lambda r: r["model"])
        top = max(cands, key=lambda r: r["total"])
        mine = next(p["element"] for p in picks if p["is_captain"])
        mine_pts = sum(r["total_points"] for r in hist[mine] if r["round"] == g)
        for k, pts in (("model", model["actual"]), ("top", top["actual"]), ("mine", mine_pts)):
            tot[k] += pts
        L.append(f"| {g} | {names[model['id']]} | {model['actual']} | {names[top['id']]} | "
                 f"{top['actual']} | {names[mine]} | {mine_pts} |")
    L.append(f"| **Total** | | **{tot['model']}** | | **{tot['top']}** | | **{tot['mine']}** |")
    return L + ["", "Captain points shown once (not doubled)."]


def report(bs, fx, hist):
    rows = [r for g in range(FIRST, LAST + 1) for r in rows_for(bs, fx, hist, g)]
    played = [r for r in rows if r["played"]]
    L = [f"# Backtest GW{FIRST}-GW{LAST}", "",
         "Bias is mean(predicted - actual); positive means over-predicting. Population: "
         "every player with minutes before the gameweek. Injury news is not rebuilt, so "
         "the full population over-predicts absentees for every method; the played-in-GW "
         "split removes that. Fixture difficulty is today's.", ""]
    L += table("Overall", [("all", rows), ("played in GW", played)])
    L += table("By gameweek (all)", [(f"GW{g}", [r for r in rows if r["g"] == g])
                                     for g in range(FIRST, LAST + 1)])
    for label, pop in (("all", rows), ("played in GW", played)):
        L += table(f"By position ({label})",
                   [(POS_NAME[p], [r for r in pop if r["pos"] == p]) for p in POS_NAME])
        L += table(f"By price ({label})",
                   [(name, [r for r in pop if lo <= r["price"] < hi]) for lo, hi, name in BANDS])
    return L + captains(bs, fx, hist)


def tune(bs, fx, hist, specs):
    grid = {}
    for s in specs:
        name, vals = s.split("=")
        if not hasattr(M, name):
            raise SystemExit(f"no constant {name} in fplbot.model")
        grid[name] = [float(v) for v in vals.split(",")]
    base = {k: getattr(M, k) for k in grid}
    print(f"| {' | '.join(grid)} | fit RMSE | fit bias | hold RMSE | hold bias |")
    print("|" + "---|" * (len(grid) + 4))
    for combo in itertools.product(*grid.values()):
        for k, v in zip(grid, combo):
            setattr(M, k, v)
        res = []
        for gws in (FIT, HOLD):
            rs = [r for g in gws for r in rows_for(bs, fx, hist, g)]
            res += [stats(rs, "model")[1], stats(rs, "model")[0]]
        mark = " (current)" if all(v == base[k] for k, v in zip(grid, combo)) else ""
        print(f"| {' | '.join(f'{v:g}' for v in combo)}{mark} | {res[0]:.3f} | {res[1]:+.3f} "
              f"| {res[2]:.3f} | {res[3]:+.3f} |")
    for k, v in base.items():
        setattr(M, k, v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tune", nargs="*", help="CONST=v1,v2,... grids over fplbot.model constants")
    args = ap.parse_args()
    bs, fx, hist = load()
    bad = self_check(bs, hist)
    if bad:
        raise SystemExit("rebuild does not match live totals:\n  " + "\n  ".join(bad[:10]))
    if args.tune:
        tune(bs, fx, hist, args.tune)
    else:
        print("\n".join(report(bs, fx, hist)))


if __name__ == "__main__":
    main()
