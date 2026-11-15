# Next

State carried over from the old Mac on 2026-11-12.

## Next steps
1. On request: backtest GW4 to GW10 with no lookahead. Baselines are season points per game and last four games. Report bias by position and price, tune at most two constants. Needs element-summary history for all ~670 players (`data/` is not in git). Done when the report compares model captain against always-top-scorer.
2. Low value, unfixed: return dates are compared with the GW deadline, not kickoff; the "rolling the transfer" hint shows at five free transfers; `week --stdout` still writes `reports/GWnn.md` (`run.py:191`).

## Gotchas
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
