# Next

State carried over from the old Mac on 2026-11-12.

## Next steps
1. Low value, unfixed: return dates are compared with the GW deadline, not kickoff; the "rolling the transfer" hint shows at five free transfers; `week --stdout` still writes `reports/GWnn.md` (`run.py:191`).

## Gotchas
- Backtest (`python3 backtest.py`, GW4-10, 2026-11-15): model RMSE 2.60 vs 2.94 season PPG and 2.79 last-four; captain picks 49 pts vs 29 always-top-scorer. Grids over SHRINK_90S, REGRESS_W, SHRINK_STARTS are flat on GW8-10 holdout, so constants unchanged. Needs the element-summary cache in `data/`.
- python.org Python 3.14 has no CA bundle: fetches fail with CERTIFICATE_VERIFY_FAILED. Run `SSL_CERT_FILE=/etc/ssl/cert.pem ./weekly.sh`, or run `Install Certificates.command` once.
- `transfers` lists paired moves only when a buy is unaffordable as a single; club-cap-blocked pairs and the weekly report are not covered.
- `sync` before a gameweek kicks off saves the previous gameweek's squad. During a live Free Hit it saves the squad from gameweek minus one.
- Runs while a gameweek is live undercount minutes for players yet to play (`sample_games` counts the live gameweek). Decide transfers after the last fixture.
- Bootstrap `strength_attack_*` and `strength_defence_*` are all 0; only `strength_overall_*` is filled, so the fixture term uses FDR only.
- Before a deadline the public `entry/{id}/transfers/` is empty, so the pending-transfers subtraction is dead code.
- Only "until D Mon" and "Expected back D Mon" flag texts parse a return date (`fplbot/model.py:42`).
- `--lock A --lock B` keeps only B; use `--lock A B`. Ambiguous names take `Name@CLUB`.
- `player_ids` in `my_squad.json` is informational; `players` is the truth.
- The gate protects `checks.sh`, `scenarios/`, `.githooks/` and `.claude/settings.json`: changes to them commit only with `GATE_EDIT=1`, set by hand on the command line.
- zsh expands `echo ======X`; quote separators in shell calls. No ruff or pyflakes installed.
