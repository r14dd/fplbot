"""End-to-end scenarios run against a frozen data snapshot.

Each one drives the CLI the way a real run does and checks the output against
the FPL rules. The rules are written out here on purpose and never imported
from fplbot: a bug in the app must not be able to approve itself.

The snapshot freezes the data of one day, so these check that the rules hold,
not that the picks are good.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import run  # noqa: E402
from fplbot import api  # noqa: E402

# ---- the rules, restated -------------------------------------------------
SQUAD_QUOTA = {1: 2, 2: 5, 3: 5, 4: 3}      # GKP, DEF, MID, FWD
MAX_PER_CLUB = 3
BUDGET_TENTHS = 1000                        # £100.0m
XI_LIMITS = {"DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
HIT = 4.0                                   # points per transfer beyond the free ones
POS = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}

BOOT = json.loads((HERE / "snapshot" / "bootstrap-static.json").read_text())
CLUB = {t["id"]: t["short_name"] for t in BOOT["teams"]}
BY_ID = {e["id"]: e for e in BOOT["elements"]}
BY_NAME_CLUB = {(e["web_name"], CLUB[e["team"]]): e for e in BOOT["elements"]}
SQUAD = json.loads((HERE / "squad.json").read_text())
SQUAD_IDS = [BY_NAME_CLUB[tuple(tok.split("@"))]["id"] for tok in SQUAD["players"]]
SELL = {int(k): v for k, v in SQUAD["sell_prices"].items()}

FLAG = re.compile(r" (\*\*.*\*\*|_\(thin data\)_|\(C\)|\(V\))$")


def no_network(*_a, **_k):
    raise AssertionError("scenario touched the network; the snapshot must be enough")


class Scenarios(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="fpl-scenarios-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cache = tmp / "cache"
        cache.mkdir()
        for f in (HERE / "snapshot").glob("*.json"):
            shutil.copyfile(f, cache / f.name)   # fresh mtime, so the cache counts as current
        self.squad_file = tmp / "squad_under_test.json"
        shutil.copyfile(HERE / "squad.json", self.squad_file)
        for p in (
            mock.patch.object(api, "CACHE_DIR", cache),
            mock.patch.object(run, "SQUAD_FILE", self.squad_file),
            mock.patch.object(run, "REPORTS", tmp / "reports"),
            mock.patch.object(urllib.request, "urlopen", no_network),
        ):
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv: str) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["run.py", *argv]), contextlib.redirect_stdout(out):
            run.main()
        return out.getvalue()

    def assert_legal_squad(self, ids: list[int], budget_tenths: int):
        self.assertEqual(len(ids), 15)
        self.assertEqual(len(set(ids)), 15, "a player appears twice")
        self.assertEqual(Counter(BY_ID[i]["element_type"] for i in ids), Counter(SQUAD_QUOTA))
        clubs = Counter(BY_ID[i]["team"] for i in ids)
        self.assertLessEqual(max(clubs.values()), MAX_PER_CLUB, f"club cap broken: {clubs}")
        self.assertLessEqual(sum(BY_ID[i]["now_cost"] for i in ids), budget_tenths)

    def assert_legal_xi(self, formation: str, rows: list[tuple[str, str, str]]):
        """rows are (position, name, club) for the eleven starters."""
        self.assertEqual(len(rows), 11)
        self.assertEqual(len(set(rows)), 11)
        count = Counter(pos for pos, _, _ in rows)
        self.assertEqual(count["GKP"], 1)
        for pos, (lo, hi) in XI_LIMITS.items():
            self.assertTrue(lo <= count[pos] <= hi, f"{count[pos]} {pos} in the XI")
        self.assertEqual(formation, f"{count['DEF']}-{count['MID']}-{count['FWD']}")

    # ---- 1. a squad built from scratch is a legal squad -------------------

    def test_squad_from_scratch_is_legal(self):
        self.cli("squad", "--save")
        saved = json.loads(self.squad_file.read_text())
        self.assert_legal_squad(saved["player_ids"], BUDGET_TENTHS)
        cost = sum(BY_ID[i]["now_cost"] for i in saved["player_ids"])
        self.assertAlmostEqual(saved["bank"], (BUDGET_TENTHS - cost) / 10, places=1)

    def test_squad_respects_a_smaller_budget(self):
        self.cli("squad", "--budget", "85", "--save")
        self.assert_legal_squad(json.loads(self.squad_file.read_text())["player_ids"], 850)

    # ---- 2. the weekly lineup is a legal XI with a sane captain -----------

    def week(self, *flags: str):
        md = self.cli("week", "--stdout", *flags)
        head = re.search(
            r"^## Your GW\d+ XI \((\d-\d-\d)\) — ([\d.]+) xP \(XI ([\d.]+) \+ ([\d.]+) (triple )?captain\)$",
            md, re.M)
        self.assertIsNotNone(head, "XI header missing from the report")
        xi_block = md[head.end():md.index("**Captain:**")]
        rows = []
        for line in xi_block.splitlines():
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 7 and cells[0] in POS:
                rows.append((cells[0], FLAG.sub("", cells[1]), cells[2], float(cells[4])))
        cap = re.search(r"^\*\*Captain:\*\* (.+) \((\w+)\) — ([\d.]+) xP", md, re.M)
        vice = re.search(r"^\*\*Vice:\*\* (.+) \((\w+)\)$", md, re.M)
        bench = re.findall(r"^\d\. \[(GK|\d)\] (.+) \((\w+), [\d.]+\) — [\d.]+ xP$", md, re.M)
        return head, rows, cap, vice, bench

    def test_week_lineup_is_legal(self):
        head, rows, cap, vice, bench = self.week()
        self.assert_legal_xi(head.group(1), [r[:3] for r in rows])

        xi = {(name, club) for _, name, club, _ in rows}
        captain, vc = (cap.group(1), cap.group(2)), (vice.group(1), vice.group(2))
        self.assertIn(captain, xi, "captain is not in the XI")
        self.assertIn(vc, xi, "vice-captain is not in the XI")
        self.assertNotEqual(captain, vc)
        cap_xp = float(cap.group(3))
        self.assertEqual(cap_xp, max(xp for *_, xp in rows), "captain is not the top projection")

        self.assertEqual(len(bench), 4)
        self.assertEqual(bench[0][0], "GK", "the spare keeper must sit first on the bench")
        on_bench = {(name, club) for _, name, club in bench}
        ids = sorted(BY_NAME_CLUB[k]["id"] for k in xi | on_bench)
        self.assertEqual(ids, sorted(SQUAD_IDS), "XI + bench is not the saved squad")

        total, xi_pts, extra = (float(head.group(i)) for i in (2, 3, 4))
        self.assertAlmostEqual(total, xi_pts + extra, delta=0.11)
        self.assertAlmostEqual(extra, cap_xp, delta=0.11)      # captain counts twice in total

    def test_lineup_keeps_three_defenders_even_when_they_are_duds(self):
        # Four of the five defenders have not played a minute, so the tempting
        # lineup is two at the back. The rules still demand three.
        duds = [e for e in BOOT["elements"]
                if e["element_type"] == 2 and e["minutes"] == 0 and e["id"] not in SQUAD_IDS][:4]
        d = json.loads(self.squad_file.read_text())
        defenders = [t for t in d["players"] if BY_NAME_CLUB[tuple(t.split("@"))]["element_type"] == 2]
        for old, new in zip(defenders[1:], duds):
            d["players"][d["players"].index(old)] = f"{new['web_name']}@{CLUB[new['team']]}"
        self.squad_file.write_text(json.dumps(d))
        head, rows, *_ = self.week()
        self.assert_legal_xi(head.group(1), [r[:3] for r in rows])

    def test_triple_captain_adds_the_captain_twice(self):
        head, _, cap, _, _ = self.week("--tc")
        self.assertEqual(head.group(5), "triple ")
        self.assertAlmostEqual(float(head.group(4)), 2 * float(cap.group(3)), delta=0.2)

    # ---- 3. every suggested transfer is one you could actually make -------

    def transfers(self):
        out = self.cli("transfers", "--top", "25")
        moves = re.findall(
            r"OUT (.+?)\s+-> IN (.+?)\s+\((\w+), £([\d.]+)\)\s+([+-][\d.]+) xP\s+"
            r"net ([+-][\d.]+)\s+ITB £(-?[\d.]+)m", out)
        self.assertEqual(len(moves), 25, "transfer table did not parse")
        return moves

    def test_transfers_are_affordable_and_net_of_hits(self):
        self.assertEqual(SQUAD["free_transfers"], 0)   # the fixture squad has no free transfer
        by_name = {BY_ID[i]["web_name"]: BY_ID[i] for i in SQUAD_IDS}
        nets = []
        for out_name, in_name, in_club, in_price, gain, net, itb in self.transfers():
            sold, bought = by_name[out_name], BY_NAME_CLUB[(in_name, in_club)]
            self.assertNotIn(bought["id"], SQUAD_IDS)
            self.assertEqual(bought["element_type"], sold["element_type"], "swap changes position")
            after = [i for i in SQUAD_IDS if i != sold["id"]] + [bought["id"]]
            self.assertLessEqual(max(Counter(BY_ID[i]["team"] for i in after).values()), MAX_PER_CLUB)
            # you sell at the selling price, not the current price
            left = SQUAD["bank"] + SELL[sold["id"]] - bought["now_cost"] / 10
            self.assertGreaterEqual(float(itb), 0.0, "move costs more than you have")
            self.assertAlmostEqual(float(itb), left, delta=0.051)
            self.assertAlmostEqual(float(net), float(gain) - HIT, delta=0.011)
            nets.append(float(net))
        self.assertEqual(nets, sorted(nets, reverse=True), "moves are not ranked by net gain")

    def test_a_free_transfer_costs_no_hit(self):
        d = json.loads(self.squad_file.read_text())
        d["free_transfers"] = 1
        self.squad_file.write_text(json.dumps(d))
        for *_, gain, net, _itb in self.transfers():
            self.assertEqual(net, gain)

    # ---- 4. a Free Hit squad fits the selling value ----------------------

    def test_freehit_fits_selling_value(self):
        out = self.cli("freehit")
        budget = float(re.search(r"Free Hit GW\d+: budget £([\d.]+)m = selling value \+ bank", out).group(1))
        self.assertAlmostEqual(budget, SQUAD["bank"] + sum(SELL[i] for i in SQUAD_IDS), delta=0.051)

        formation = re.search(r"XI \((\d-\d-\d)\)", out).group(1)
        xi = re.findall(r"^    (GKP|DEF|MID|FWD)  (.+?)\s+([A-Z]{3})\s+£([\d.]+)", out, re.M)
        rows = [(pos, FLAG.sub("", name), club) for pos, name, club, _ in xi]
        self.assert_legal_xi(formation, rows)
        clubs = Counter(club for _, _, club in rows)
        self.assertLessEqual(max(clubs.values()), MAX_PER_CLUB)

        bench = re.findall(r"\(£([\d.]+)\)", re.search(r"^  Bench: (.+)$", out, re.M).group(1))
        self.assertEqual(len(bench), 4)
        spent = sum(float(p) for *_, p in xi) + sum(float(p) for p in bench)
        cost, itb = (float(x) for x in re.search(r"Cost £([\d.]+)m \| ITB £(-?[\d.]+)m", out).groups())
        self.assertAlmostEqual(cost, spent, delta=0.051)
        self.assertLessEqual(cost, budget + 0.051, "Free Hit squad costs more than the selling value")
        self.assertGreaterEqual(itb, 0.0)


if __name__ == "__main__":
    unittest.main()
