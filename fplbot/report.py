"""Weekly markdown report."""

from __future__ import annotations

from datetime import datetime, timezone

from .model import POS_NAME, Model, Player
from .optimize import best_xi, suggest_transfers, week_score


def _deadline(model: Model, gw: int) -> str:
    for e in model.events:
        if e["id"] == gw:
            dt = datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))
            delta = dt - datetime.now(timezone.utc)
            days, rem = divmod(max(0, int(delta.total_seconds())), 86400)
            hours = rem // 3600
            return f"{dt:%a %d %b %H:%M} UTC ({days}d {hours}h away)"
    return "unknown"


def _row(p: Player, model: Model, gws: list[int]) -> str:
    flag = ""
    if p.availability == 0:
        flag = " **OUT**"
    elif p.availability < 1:
        flag = f" **{int(p.availability * 100)}%**"
    elif p.data_thin:
        flag = " _(thin data)_"
    xp = model.horizon(p, gws)
    return (
        f"| {p.pos_name} | {p.name}{flag} | {p.team_short} | {p.price:.1f} | "
        f"{p.xpts_by_gw.get(gws[0], 0):.1f} | {xp:.1f} | {p.selected_by:.1f}% |"
    )


HEAD = (
    "| Pos | Player | Club | £ | xP next | xP run | Own |\n"
    "|-----|--------|------|---|---------|--------|-----|"
)


def fixture_ticker(model: Model, gws: list[int], limit: int = 10) -> str:
    rows = []
    for tid, t in model.teams.items():
        cells, total, count = [], 0, 0
        for gw in gws:
            fxs = model.team_fixtures(gw).get(tid, [])
            if not fxs:
                cells.append("—")
                continue
            for opp, home, diff in fxs:
                opp_s = model.teams[opp]["short_name"]
                cells.append(f"{opp_s}{'(H)' if home else '(A)'}[{diff}]")
                total += diff
                count += 1
        avg = total / count if count else 9
        rows.append((avg, t["short_name"], cells))
    rows.sort()
    out = [f"| Club | Avg | {' | '.join('GW' + str(g) for g in gws)} |",
           "|------|-----|" + "|".join(["---"] * len(gws)) + "|"]
    for avg, name, cells in rows[:limit]:
        padded = (cells + ["—"] * len(gws))[: len(gws)]
        out.append(f"| {name} | {avg:.2f} | {' | '.join(padded)} |")
    return "\n".join(out)


def build(
    model: Model,
    gws: list[int],
    squad: list[Player] | None = None,
    bank: float = 0.0,
    free_transfers: int = 1,
    sell_prices: dict[int, float] | None = None,
    tc: bool = False,
) -> str:
    gw = gws[0]
    L = []
    L.append(f"# FPL report — Gameweek {gw}")
    L.append("")
    L.append(f"**Deadline:** {_deadline(model, gw)}  ")
    L.append(f"**Horizon:** GW{gws[0]}–GW{gws[-1]}  ")
    L.append(f"**Gameweeks completed:** {model.finished_gws}")
    L.append("")

    if model.preseason:
        L.append(
            "> ⚠️ **Pre-season.** The API's per-player totals are still LAST "
            "season's, shown against each player's NEW club. Treat every points "
            "figure as 'what this player did wherever they played last year', "
            "not as evidence about their current team. Promoted-club players "
            "have no Premier League data at all and will project near zero."
        )
        L.append("")

    bd = model.blanks_and_doubles(gws)
    if bd:
        L.append("## Blanks & doubles in range")
        for g, d in sorted(bd.items()):
            bits = []
            if d["blank"]:
                bits.append(f"blank: {', '.join(d['blank'])}")
            if d["double"]:
                bits.append(f"**double: {', '.join(d['double'])}**")
            L.append(f"- GW{g} — {'; '.join(bits)}")
        L.append("")

    if squad:
        # The run horizon only drives transfers; the lineup is this week's call.
        nxt = week_score(model, gws)
        lu = best_xi(squad, nxt)
        extra = (2 if tc else 1) * nxt(lu.captain)
        L.append(f"## Your GW{gw} XI ({lu.formation}) — {lu.points + extra:.1f} xP "
                 f"(XI {lu.points:.1f} + {extra:.1f} {'triple ' if tc else ''}captain)")
        L.append("")
        L.append(HEAD)
        order = {1: 0, 2: 1, 3: 2, 4: 3}
        for p in sorted(lu.xi, key=lambda x: (order[x.pos], -nxt(x))):
            L.append(_row(p, model, gws))
        L.append("")
        L.append(f"**Captain:** {lu.captain.name} ({lu.captain.team_short}) — "
                 f"{nxt(lu.captain):.1f} xP  ")
        L.append(f"**Vice:** {lu.vice.name} ({lu.vice.team_short})")
        L.append("")
        L.append("**Bench** (order matters — autosubs only fire for a starter "
                 "who plays zero minutes):")
        for i, p in enumerate(lu.bench, 1):
            tag = "GK" if p.pos == 1 else str(i - 1)
            L.append(f"{i}. [{tag}] {p.name} ({p.team_short}, {p.price:.1f}) — "
                     f"{nxt(p):.1f} xP")
        L.append("")

        alerts = [p for p in squad if p.availability < 1]
        if alerts:
            L.append("## ⚠️ Availability alerts")
            for p in alerts:
                L.append(f"- **{p.name}** ({p.team_short}) — {p.status}, "
                         f"{p.news or 'no detail'}")
            L.append("")

        moves = suggest_transfers(
            model, squad, gws, bank, free_transfers, sell_prices
        )
        if moves:
            L.append(f"## Transfer options (bank £{bank:.1f}m, "
                     f"{free_transfers} free)")
            L.append("")
            L.append("| Out | In | Gain (xP) | Net of hit | Bank after |")
            L.append("|-----|----|-----------|------------|------------|")
            for m in moves:
                L.append(
                    f"| {m['out'].name} ({m['out'].price:.1f}) | "
                    f"{m['in'].name} ({m['in'].team_short}, {m['in'].price:.1f}) | "
                    f"{m['gain']:+.2f} | {m['net']:+.2f} | £{m['bank_after']:.1f}m |"
                )
            L.append("")
            if moves[0]["net"] < 1.0:
                L.append("_Best move nets under 1 xP after the 4-point hit — "
                         "don't transfer._" if free_transfers == 0 else
                         "_Best available move gains under 1 xP — rolling the "
                         "transfer is likely better than using it._")
            L.append("")

    L.append("## Best value by position")
    L.append("")
    for pos in POS_NAME:
        cands = [
            p
            for p in model.players
            if p.pos == pos and model.horizon(p, gws) > 0 and not p.data_thin
        ]
        cands.sort(key=lambda p: model.horizon(p, gws), reverse=True)
        L.append(f"### {POS_NAME[pos]}")
        L.append(HEAD)
        for p in cands[:8]:
            L.append(_row(p, model, gws))
        L.append("")

    L.append("## Fixture ticker (easiest runs)")
    L.append("")
    L.append(fixture_ticker(model, gws))
    L.append("")
    L.append("---")
    L.append(
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
        "Projections are a heuristic: shrunk points-per-90, adjusted for "
        "expected minutes, fixture difficulty and availability. Not a "
        "substitute for team-news checks before the deadline._"
    )
    return "\n".join(L)
