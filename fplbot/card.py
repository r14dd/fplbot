"""Player cards: every factor that goes into a buy / don't-buy call, side by side.

Numbers come from the player's own element-summary history (this season only)
and fixtures.json. Fixture ease is the API's FDR (1 easy .. 5 hard) split into
the near run and the whole horizon the squad has to survive.
"""

from __future__ import annotations

from fplbot import api
from fplbot.model import Model, Player

DEFCON_THRESHOLD = {2: 10, 3: 12, 4: 12}   # defensive actions for the +2


def _history(p: Player) -> list[dict]:
    rows = api.element_summary(p.id)["history"]
    # unplayed fixtures already have a row; they carry no score yet
    return [r for r in rows if r.get("team_h_score") is not None]


def _team_record(model: Model, team: int) -> dict:
    gf = ga = cs = played = 0
    for fx in model.fixtures:
        if fx.get("team_h_score") is None or team not in (fx["team_h"], fx["team_a"]):
            continue
        home = fx["team_h"] == team
        f, a = (fx["team_h_score"], fx["team_a_score"]) if home else (fx["team_a_score"], fx["team_h_score"])
        gf, ga, cs, played = gf + f, ga + a, cs + (a == 0), played + 1
    return {"played": played, "gf": gf, "ga": ga, "cs": cs}


def _fixtures(model: Model, team: int, a: int, b: int) -> list[tuple[int, str, int]]:
    out = []
    for gw in range(a, b + 1):
        for opp, home, diff in model.team_fixtures(gw).get(team, []):
            out.append((gw, f"{model.teams[opp]['short_name']}{'(H)' if home else '(a)'}", diff))
    return out


def card(model: Model, p: Player, near: tuple[int, int], far: tuple[int, int]) -> dict:
    rows = _history(p)
    played = [r for r in rows if r["minutes"] > 0]
    mins = sum(r["minutes"] for r in rows)
    pts = sum(r["total_points"] for r in rows)
    xg = sum(float(r["expected_goals"]) for r in rows)
    xa = sum(float(r["expected_assists"]) for r in rows)
    thr = DEFCON_THRESHOLD.get(p.pos, 999)
    defcon = sum(1 for r in rows if (r.get("defensive_contribution") or 0) >= thr)
    # xGC from full-90 appearances is the team's xGA in those games
    xgc = [float(r["expected_goals_conceded"]) for r in rows if r["minutes"] >= 90]
    team = _team_record(model, p.team)
    near_fx = _fixtures(model, p.team, *near)
    far_fx = _fixtures(model, p.team, *far)
    avg = lambda fx: sum(d for _, _, d in fx) / len(fx) if fx else 0.0
    xp = lambda a, b: sum(p.xpts_by_gw.get(g, 0.0) for g in range(a, b + 1))
    return {
        "name": p.name, "club": p.team_short, "pos": p.pos_name, "price": p.price,
        "own": p.selected_by, "status": ("fit" if p.availability == 1 else f"{int(p.availability*100)}%"),
        "news": p.news[:40],
        "gw_pts": [r["total_points"] for r in rows],
        "pts_gw": pts / len(rows) if rows else 0.0,
        "pts_90": pts / mins * 90 if mins else 0.0,
        "value": (pts / len(rows) / p.price) if rows else 0.0,
        "starts": f"{sum(1 for r in rows if r['starts'])}/{len(rows)}",
        "mins_start": mins / max(1, sum(1 for r in rows if r["starts"])),
        "gi": f"{sum(r['goals_scored'] for r in rows)}g {sum(r['assists'] for r in rows)}a",
        "xgi": f"{xg:.1f}xG {xa:.1f}xA",
        "xgi_90": (xg + xa) / mins * 90 if mins else 0.0,
        "defcon": f"{defcon}/{len(played)}" if p.pos in DEFCON_THRESHOLD else "-",
        "bonus": sum(r["bonus"] for r in rows),
        "returns": sum(1 for r in rows if r["total_points"] >= 6),
        "team": f"{team['gf']}-{team['ga']} in {team['played']}, {team['cs']} CS",
        "team_xga_90": sum(xgc) / len(xgc) if xgc else None,
        "near": " ".join(f"{o}{d}" for _, o, d in near_fx),
        "near_fdr": avg(near_fx),
        "far_fdr": avg(far_fx),
        "far_easy": sum(1 for _, _, d in far_fx if d <= 2),
        "far_hard": sum(1 for _, _, d in far_fx if d >= 4),
        "xp_near": xp(*near),
        "xp_far": xp(*far),
    }


ROWS = [
    ("club / pos",        lambda c: f"{c['club']} {c['pos']}"),
    ("price",             lambda c: f"£{c['price']:.1f}"),
    ("owned",             lambda c: f"{c['own']:.1f}%"),
    ("status",            lambda c: c["status"] + (f" {c['news']}" if c["news"] else "")),
    ("pts by GW",         lambda c: " ".join(str(x) for x in c["gw_pts"])),
    ("pts / GW",          lambda c: f"{c['pts_gw']:.1f}"),
    ("pts / 90",          lambda c: f"{c['pts_90']:.1f}"),
    ("pts/GW per £m",     lambda c: f"{c['value']:.2f}"),
    ("starts",            lambda c: f"{c['starts']} ({c['mins_start']:.0f} min)"),
    ("goals+assists",     lambda c: c["gi"]),
    ("expected",          lambda c: c["xgi"]),
    ("xGI / 90",          lambda c: f"{c['xgi_90']:.2f}"),
    ("DefCon hits",       lambda c: c["defcon"]),
    ("bonus",             lambda c: str(c["bonus"])),
    ("6+ pt hauls",       lambda c: str(c["returns"])),
    ("team GF-GA",        lambda c: c["team"]),
    ("team xGA / game",   lambda c: "-" if c["team_xga_90"] is None else f"{c['team_xga_90']:.2f}"),
    ("NEAR fixtures",     lambda c: c["near"]),
    ("  avg FDR",         lambda c: f"{c['near_fdr']:.2f}"),
    ("FAR avg FDR",       lambda c: f"{c['far_fdr']:.2f}"),
    ("  easy / hard",     lambda c: f"{c['far_easy']} easy, {c['far_hard']} hard"),
    ("model xP near",     lambda c: f"{c['xp_near']:.1f}"),
    ("model xP far",      lambda c: f"{c['xp_far']:.1f}"),
]


def render(cards: list[dict], near: tuple[int, int], far: tuple[int, int]) -> str:
    cols = [c["name"] for c in cards]
    cells = [[fn(c) for c in cards] for _, fn in ROWS]
    widths = [max(len(n), *(len(r[i]) for r in cells)) for i, n in enumerate(cols)]
    labels = [lbl for lbl, _ in ROWS]
    labels[labels.index("NEAR fixtures")] = f"GW{near[0]}-{near[1]} fixtures"
    labels[labels.index("FAR avg FDR")] = f"GW{far[0]}-{far[1]} avg FDR"
    labels[labels.index("model xP near")] = f"model xP GW{near[0]}-{near[1]}"
    labels[labels.index("model xP far")] = f"model xP GW{far[0]}-{far[1]}"
    lw = max(len(l) for l in labels)
    line = lambda l, r: f"  {l:<{lw}}  " + "  ".join(f"{v:<{w}}" for v, w in zip(r, widths))
    out = [line("", cols), line("", ["-" * w for w in widths])]
    out += [line(l, r) for l, r in zip(labels, cells)]
    out.append("")
    out.append("  FDR 1=easiest 5=hardest. DefCon = 10+ defensive actions (DEF) / 12+ (MID,FWD) = +2 pts.")
    out.append("  Model xP is fixture-driven and shrinks everyone toward average; trust pts/GW more this early.")
    return "\n".join(out)
