# fplbot

Fantasy Premier League assistant for the terminal. Pulls live data from the
public FPL API, projects points for every player, and picks the best legal
squad, XI, captain and transfers it can find. Python stdlib only — no
dependencies, no credentials.

## Quick start

```bash
python3 run.py squad --save        # build an optimal 15 from scratch
python3 run.py sync --entry 1234567   # or pull your real team (from GW1 on)
python3 run.py week                # write reports/GWnn.md
```

Your entry ID is in your team URL: `fantasy.premierleague.com/entry/<ID>/...`.
Before GW1 starts the API hides picks, so edit the `players` list in
`my_squad.json` by hand instead (`Name`, or `Name@CLUB` if ambiguous).

## Commands

| Command | What it does |
|---------|--------------|
| `squad` | Build a 15 from scratch. `--save` writes `my_squad.json`. |
| `week` | Full markdown report: XI, captain, bench order, transfer table. |
| `transfers` | Just the ranked transfer suggestions. |
| `sync --entry ID` | Pull your squad, bank, free transfers and selling prices from FPL. |
| `freehit --gw N` | Best one-week squad for a Free Hit, budgeted on your selling value. |
| `rank` | Mini-league finish odds for the next gameweek. |
| `compare A B` | Side-by-side factor cards for players. |
| `refresh` | Force re-download (data is cached for a few hours). |

Flags: `--horizon N` plans over the next N gameweeks (default 5),
`--lock Haaland` forces players in, `--exclude Saka` keeps them out,
`--budget 98.5` for a non-standard budget.

## How it works

Expected points per player per gameweek is shrunk points-per-90 × expected
minutes × fixture multiplier × availability, summed over the horizon. Squads
and transfers are optimized against that. It's a transparent heuristic, not
ML — read `fplbot/model.py` to see every number.

## Limitations

- Early-season projections are noisy (small samples).
- No chip strategy (wildcard / bench boost / triple captain timing).
- No price-change prediction.
- The public API hides selling prices; `sync` rebuilds them from your
  transfer record (half of any rise, rounded down), which assumes the
  starting squad was bought at GW1 prices.
