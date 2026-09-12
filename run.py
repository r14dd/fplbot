#!/usr/bin/env python3
"""CLI for the FPL assistant.

    python3 run.py squad              build an optimal 15 from scratch
    python3 run.py week               full weekly report for your squad
    python3 run.py transfers          just the transfer table
    python3 run.py sync --entry 1234  pull your real squad from the FPL site
    python3 run.py refresh            force re-download of all data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fplbot import api
from fplbot.model import Model, Player
from fplbot.optimize import BUDGET, best_xi, pick_squad, suggest_transfers
from fplbot.report import build as build_report

ROOT = Path(__file__).resolve().parent
SQUAD_FILE = ROOT / "my_squad.json"
REPORTS = ROOT / "reports"


def load_model(force: bool = False) -> Model:
    model = Model(api.bootstrap(force), api.fixtures(force))
    if model.data_wiped:
        raise SystemExit(
            "the FPL API currently reports zero minutes for every player.\n"
            "  This happens in the short window where last season's totals have been\n"
            "  cleared but no match of the new season has finished yet. There is no\n"
            "  data to project from, so any squad produced now would be meaningless.\n"
            "  Re-run once the first fixtures have been played."
        )
    return model


def resolve(model: Model, token: str) -> Player:
    """Find a player by 'Name' or the unambiguous 'Name@CLUB' form."""
    token = str(token).strip()
    if token.isdigit():
        hit = [p for p in model.players if p.id == int(token)]
        if hit:
            return hit[0]
        raise SystemExit(f"no player with id {token}")

    club = None
    if "@" in token:
        token, club = (x.strip() for x in token.split("@", 1))

    hits = [p for p in model.players if p.name.lower() == token.lower()]
    if club:
        hits = [p for p in hits if p.team_short.lower() == club.lower()]
    if not hits:
        near = [p for p in model.players if token.lower() in p.name.lower()][:6]
        hint = ", ".join(f"{p.name}@{p.team_short}" for p in near)
        raise SystemExit(f"no player matched {token!r}." + (f" Did you mean: {hint}" if hint else ""))
    if len(hits) > 1:
        opts = ", ".join(f"{p.name}@{p.team_short} ({p.pos_name} £{p.price})" for p in hits)
        raise SystemExit(f"{token!r} is ambiguous — use one of: {opts}")
    return hits[0]


def horizon_gws(model: Model, n: int) -> list[int]:
    start = model.next_gw
    if start is None:
        raise SystemExit("season is over — no upcoming gameweek")
    return [g for g in range(start, min(start + n, 39))]


def load_squad(model: Model) -> tuple[list[Player], float, int, dict]:
    if not SQUAD_FILE.exists():
        raise SystemExit(
            f"no squad saved yet. Run 'python3 run.py squad --save' to create one, "
            f"or 'python3 run.py sync --entry <id>' once you have a team."
        )
    d = json.loads(SQUAD_FILE.read_text())
    players = []
    for tok in d["players"]:
        try:
            players.append(resolve(model, tok))
        except SystemExit as exc:
            # 'players' is hand-editable, so player_ids can drift out of step
            # with it — falling back to an id by position could silently load
            # somebody else's squad. Fail loudly and name the bad entry.
            raise SystemExit(
                f"{exc}\n  ^ this came from the 'players' list in my_squad.json. "
                f"If they changed club, fix that entry (use Name@CLUB)."
            )
    return players, float(d.get("bank", 0)), int(d.get("free_transfers", 1)), d.get("sell_prices", {})


def save_squad(squad: list[Player], bank: float, ft: int, sell: dict | None = None) -> None:
    SQUAD_FILE.write_text(
        json.dumps(
            {
                "_comment": "Edit 'players' freely — it is the source of truth. "
                            "Use Name@CLUB if a name is ambiguous. 'player_ids' is "
                            "informational only and is not read back.",
                "bank": round(bank, 1),
                "free_transfers": ft,
                "players": [f"{p.name}@{p.team_short}" for p in squad],
                "player_ids": [p.id for p in squad],
                "sell_prices": sell or {},
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"  saved -> {SQUAD_FILE}")


def show_squad(model: Model, squad: list[Player], gws: list[int]) -> None:
    lu = best_xi(squad, lambda p: model.horizon(p, gws))
    cost = sum(p.price for p in squad)
    print(f"\n  XI ({lu.formation}) — {lu.points:.1f} xP over GW{gws[0]}-{gws[-1]}")
    order = {1: 0, 2: 1, 3: 2, 4: 3}
    for p in sorted(lu.xi, key=lambda x: (order[x.pos], -model.horizon(x, gws))):
        star = " (C)" if p.id == lu.captain.id else " (V)" if p.id == lu.vice.id else ""
        print(f"    {p.pos_name}  {p.name+star:<20} {p.team_short:<4} "
              f"£{p.price:<5.1f} {model.horizon(p, gws):5.1f} xP")
    print("  Bench:", ", ".join(f"{p.name} (£{p.price:.1f})" for p in lu.bench))
    print(f"  Cost £{cost:.1f}m | ITB £{BUDGET - cost:.1f}m")


def cmd_squad(args) -> None:
    model = load_model(args.refresh)
    gws = horizon_gws(model, args.horizon)
    model.project(gws)
    locked = [resolve(model, t) for t in (args.lock or [])]
    excluded = {resolve(model, t).id for t in (args.exclude or [])}
    print(f"  optimising over GW{gws[0]}-{gws[-1]}, budget £{args.budget:.1f}m ...")
    squad = pick_squad(model, gws, budget=args.budget, exclude=excluded, locked=locked)
    show_squad(model, squad, gws)
    if args.save:
        save_squad(squad, BUDGET - sum(p.price for p in squad), 1)


def cmd_week(args) -> None:
    model = load_model(args.refresh)
    gws = horizon_gws(model, args.horizon)
    model.project(gws)
    squad = bank = ft = sell = None
    if SQUAD_FILE.exists():
        squad, bank, ft, sell = load_squad(model)
        sell = {int(k): float(v) for k, v in (sell or {}).items()}
    md = build_report(model, gws, squad, bank or 0.0, ft or 1, sell)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"GW{gws[0]:02d}.md"
    out.write_text(md)
    print(md if args.stdout else f"  report written -> {out}")


def cmd_transfers(args) -> None:
    model = load_model(args.refresh)
    gws = horizon_gws(model, args.horizon)
    model.project(gws)
    squad, bank, ft, sell = load_squad(model)
    sell = {int(k): float(v) for k, v in (sell or {}).items()}
    moves = suggest_transfers(model, squad, gws, bank, ft, sell, top_n=args.top)
    print(f"\n  bank £{bank:.1f}m, {ft} free transfer(s), GW{gws[0]}-{gws[-1]}\n")
    for m in moves:
        print(f"    OUT {m['out'].name:<18} -> IN {m['in'].name:<18} "
              f"({m['in'].team_short}, £{m['in'].price:.1f})  "
              f"{m['gain']:+.2f} xP   ITB £{m['bank_after']:.1f}m")
    if moves and moves[0]["gain"] < 1.0:
        print("\n  Best move gains < 1 xP — rolling the transfer is likely better.")


def cmd_sync(args) -> None:
    model = load_model(args.refresh)
    if model.started_gw is None:
        raise SystemExit(
            "sync needs a gameweek that has already started.\n"
            "  The public API only exposes your picks once a gameweek is under way;\n"
            "  before the GW1 deadline that data sits behind a login, so there is\n"
            "  genuinely nothing to pull yet — this is not a bad entry id.\n\n"
            "  Until GW1 kicks off, put your team in by hand: edit the 'players'\n"
            "  list in my_squad.json (use Name@CLUB if a name is ambiguous), then\n"
            "  run 'python3 run.py week'. Come back to sync once GW1 has started."
        )
    gw = model.started_gw
    try:
        picks = api.entry_picks(args.entry, gw, force=True)
    except Exception as exc:
        raise SystemExit(f"could not read entry {args.entry} for GW{gw}: {exc}")
    ids = [p["element"] for p in picks["picks"]]
    # selling_price is only present on the authenticated my-team endpoint
    sell = {str(p["element"]): p["selling_price"] / 10
            for p in picks["picks"] if p.get("selling_price")}
    squad = [resolve(model, str(i)) for i in ids]
    bank = picks.get("entry_history", {}).get("bank", 0) / 10
    save_squad(squad, bank, 1, sell)
    gws = horizon_gws(model, args.horizon)
    model.project(gws)
    show_squad(model, squad, gws)


def cmd_refresh(args) -> None:
    api.bootstrap(force=True)
    api.fixtures(force=True)
    print("  refreshed bootstrap + fixtures")


def main() -> None:
    # Shared flags, attached to every subcommand so they can appear either
    # before or after the command name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--horizon", type=int, default=5,
                        help="gameweeks to plan over (default 5)")
    common.add_argument("--refresh", action="store_true", help="force re-download")

    ap = argparse.ArgumentParser(description="Personal FPL assistant", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("squad", parents=[common],
                       help="build an optimal squad from scratch")
    s.add_argument("--budget", type=float, default=BUDGET)
    s.add_argument("--save", action="store_true")
    s.add_argument("--lock", nargs="*", help="players that must be included")
    s.add_argument("--exclude", nargs="*", help="players to avoid")
    s.set_defaults(func=cmd_squad)

    s = sub.add_parser("week", parents=[common], help="write the weekly report")
    s.add_argument("--stdout", action="store_true")
    s.set_defaults(func=cmd_week)

    s = sub.add_parser("transfers", parents=[common], help="rank transfer options")
    s.add_argument("--top", type=int, default=10)
    s.set_defaults(func=cmd_transfers)

    s = sub.add_parser("sync", parents=[common], help="pull your squad from the FPL site")
    s.add_argument("--entry", type=int, required=True)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("refresh", parents=[common], help="force re-download of all data")
    s.set_defaults(func=cmd_refresh)

    args = ap.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
