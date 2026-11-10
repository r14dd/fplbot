"""Player projection model.

Design note — why the baseline is empirical rather than a rules engine:
the FPL API publishes which stats exist but NOT the points-per-action table,
and that table has changed between seasons. So instead of hard-coding "a
midfielder goal is 5 points", the baseline is each player's observed points
per 90. Whatever the scoring rules are, they are already baked into that
number, and the model re-calibrates automatically once real matches are
played. The only place scoring values are assumed is the xG regression
correction below, which is a small adjustment term, not the baseline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

POS_NAME = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# How much a fixture swings a player's output, by position. Defenders and
# keepers live off clean sheets so they are far more fixture-sensitive than
# forwards, who can score against anyone.
FIXTURE_SENSITIVITY = {1: 0.13, 2: 0.13, 3: 0.08, 4: 0.09}
HOME_EDGE = 0.03

# Shrinkage strength, in "90s of football". A player with 6x90 of data sits
# halfway between their own rate and the positional average.
SHRINK_90S = 6.0

# Only used for the underlying-stats correction, never for the baseline.
GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}
ASSIST_PTS = 3
REGRESS_W = 0.5  # how far to pull a player toward their xG/xA rather than actual

MIN_MINUTES_TRUSTED = 450  # below this, flag the projection as thin
START_PRIOR = 0.5          # prior probability a player starts, before evidence
SHRINK_STARTS = 4.0        # matches of evidence needed to move off that prior

# "Suspended until 5 Dec", "Knee injury - Expected back 22 Nov"
RETURN_RE = re.compile(r"(?:until|Expected back) (\d{1,2} [A-Z][a-z]{2})\b")


def _return_date(news: str, news_added: str | None) -> date | None:
    """Return date from the news text; the year comes from when it was posted."""
    m = RETURN_RE.search(news or "")
    if not m or not news_added:
        return None
    added = datetime.fromisoformat(news_added.replace("Z", "+00:00")).date()
    # A date a little before the post is stale text (already back), not next year.
    for year in (added.year, added.year + 1):
        try:
            d = datetime.strptime(f"{m.group(1)} {year}", "%d %b %Y").date()
        except ValueError:  # 29 Feb in a non-leap year
            continue
        if (d - added).days >= -180:
            return d
    return None


@dataclass
class Player:
    id: int
    name: str
    pos: int
    team: int
    team_short: str
    price: float
    minutes: int
    starts: int
    total_points: int
    goals: int
    assists: int
    xg: float
    xa: float
    status: str
    chance: float | None
    selected_by: float
    news: str
    pens_order: int | None
    news_added: str | None = None
    # derived
    pp90: float = 0.0
    exp_mins: float = 0.0
    availability: float = 1.0   # next round only, as the API means it
    return_date: date | None = None
    data_thin: bool = False
    xpts_by_gw: dict = field(default_factory=dict)
    avail_by_gw: dict = field(default_factory=dict)

    @property
    def pos_name(self) -> str:
        return POS_NAME[self.pos]

    @property
    def fit_per_game(self) -> float:
        return self.pp90 * self.exp_mins / 90.0


class Model:
    def __init__(self, bootstrap: dict, fixtures: list):
        self.bs = bootstrap
        self.fixtures = fixtures
        self.teams = {t["id"]: t for t in bootstrap["teams"]}
        self.events = bootstrap["events"]
        self.deadlines = {
            e["id"]: datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")).date()
            for e in self.events
        }
        self.formation = {
            et["id"]: (et["squad_min_play"], et["squad_max_play"])
            for et in bootstrap["element_types"]
        }
        self.squad_size = {et["id"]: 0 for et in bootstrap["element_types"]}
        self.players: list[Player] = []
        self._build()

    # ---------- season phase ----------

    @property
    def finished_gws(self) -> int:
        return sum(1 for e in self.events if e.get("finished"))

    @property
    def next_gw(self) -> int | None:
        for e in self.events:
            if e.get("is_next"):
                return e["id"]
        for e in self.events:
            if not e.get("finished"):
                return e["id"]
        return None

    @property
    def started_gw(self) -> int | None:
        """Highest gameweek that has kicked off, finished or not.

        Distinct from finished_gws: entry picks go public the moment a
        gameweek starts, but 'finished' only flips once bonus is applied,
        so gating squad sync on finished would refuse live, available data.
        """
        started = [e["id"] for e in self.events
                   if e.get("finished") or e.get("is_current")]
        return max(started) if started else None

    @property
    def preseason(self) -> bool:
        """True when bootstrap's aggregates are LAST season's carryover.

        Before GW1 the API keeps previous-season totals but already shows each
        player's NEW club, so a player's points may have been scored elsewhere.

        Keys off started_gw, not finished_gws: the aggregates reset at the
        first kickoff, while 'finished' only flips once bonus is applied.
        Using finished_gws left a multi-day window where this-season totals
        were divided by a 38-game denominator.
        """
        return self.started_gw is None

    @property
    def data_wiped(self) -> bool:
        """No usable history at all: the aggregates have been reset for the new
        season but no match has finished yet.

        Bootstrap totals reset at the season rollover, which need not coincide
        with the first finished fixture — so there can be a window (roughly the
        GW1 deadline through the first results) where every counter reads zero.
        Detect it from the data rather than the event flags, because silently
        projecting every player at zero would produce a confident, meaningless
        squad.
        """
        return self.started_gw is None and sum(p.minutes for p in self.players) == 0

    @property
    def sample_games(self) -> int:
        """How many matches the bootstrap aggregates are drawn from.

        Pre-season those aggregates are last season's, so the sample is a full
        38 games. Once the season starts they reset and the sample is only the
        gameweeks played. Anything that divides by "games available" must use
        this, not a hardcoded 38 — otherwise every rate collapses after GW1.
        """
        return 38 if self.preseason else max(1, self.started_gw)

    # ---------- player table ----------

    def _build(self) -> None:
        raw = self.bs["elements"]
        for e in raw:
            p = Player(
                id=e["id"],
                name=e["web_name"],
                pos=e["element_type"],
                team=e["team"],
                team_short=self.teams[e["team"]]["short_name"],
                price=e["now_cost"] / 10.0,
                minutes=e["minutes"],
                starts=e.get("starts") or 0,
                total_points=e["total_points"],
                goals=e.get("goals_scored") or 0,
                assists=e.get("assists") or 0,
                xg=float(e.get("expected_goals") or 0),
                xa=float(e.get("expected_assists") or 0),
                status=e.get("status", "a"),
                chance=e.get("chance_of_playing_next_round"),
                selected_by=float(e.get("selected_by_percent") or 0),
                news=e.get("news") or "",
                pens_order=e.get("penalties_order"),
                news_added=e.get("news_added"),
            )
            p.return_date = _return_date(p.news, p.news_added)
            self.players.append(p)

        priors = self._positional_priors()
        for p in self.players:
            p.pp90 = self._rate(p, priors[p.pos])
            p.exp_mins = self._minutes(p)
            p.availability = self._availability(p)
            # Thin means few minutes relative to what was actually AVAILABLE,
            # so the flag still means something in September. A flat 450 would
            # mark every player in the game as thin until GW5.
            p.data_thin = p.minutes < min(MIN_MINUTES_TRUSTED, self.sample_games * 45.0)

    def _positional_priors(self) -> dict[int, float]:
        """Average points per 90 by position, from well-established players.

        'Established' is half the minutes available so far, capped at 900. A
        flat 900 means ever-present until GW10 and nobody at all before it:
        the FWD prior fell back to 3.0 and every forward was shrunk below
        every defender.
        """
        threshold = min(900.0, self.sample_games * 45.0)
        priors = {}
        for pos in POS_NAME:
            rates = [
                p.total_points / (p.minutes / 90.0)
                for p in self.players
                if p.pos == pos and p.minutes >= threshold
            ]
            priors[pos] = sum(rates) / len(rates) if rates else 3.0
        return priors

    def _rate(self, p: Player, prior: float) -> float:
        """Points per 90, shrunk toward the positional mean, then nudged
        toward the player's underlying xG/xA rather than their finishing."""
        nineties = p.minutes / 90.0
        if nineties <= 0:
            return prior * 0.6  # unknown quantity: assume below average
        raw = (p.total_points + prior * SHRINK_90S) / (nineties + SHRINK_90S)

        # Underlying-stats correction: reward under-performers, fade the lucky.
        gpts = GOAL_PTS[p.pos]
        actual = (p.goals * gpts + p.assists * ASSIST_PTS) / nineties
        expected = (p.xg * gpts + p.xa * ASSIST_PTS) / nineties
        raw += (expected - actual) * REGRESS_W

        return max(0.0, raw)

    def _minutes(self, p: Player) -> float:
        """Expected minutes in a match the player is fit for."""
        if p.starts <= 0:
            return 8.0 if p.minutes > 0 else 0.0
        per_start = min(90.0, p.minutes / p.starts)

        # Start probability is starts over matches AVAILABLE, not over 38 --
        # in October a nailed starter has 8 starts, not 34, and dividing by 38
        # would price every player in the game as a 14-minute substitute.
        # Shrunk toward a coin flip for the same reason _rate is shrunk: one
        # start from one match is not yet evidence of a nailed starter.
        games = self.sample_games
        p_start = min(1.0, (p.starts + START_PRIOR * SHRINK_STARTS) / (games + SHRINK_STARTS))
        return p_start * per_start + (1 - p_start) * 12.0

    @staticmethod
    def _availability(p: Player) -> float:
        if p.chance is not None:
            return p.chance / 100.0
        return 1.0 if p.status == "a" else 0.0

    def availability_in(self, p: Player, gw: int) -> float:
        """The flag covers only the weeks it is about.

        A dated flag (ban, "Expected back") lasts until that date; a doubtful
        percentage is about the next round only; an undated injury or a player
        who has left the club stays out for the whole run.
        """
        if gw == self.next_gw or p.availability >= 1 or p.status == "u":
            return p.availability
        if p.return_date:
            return p.availability if self.deadlines.get(gw, date.max) < p.return_date else 1.0
        return 1.0 if p.status == "d" else p.availability

    # ---------- fixtures ----------

    def team_fixtures(self, gw: int) -> dict[int, list[tuple[int, bool, int]]]:
        """team_id -> list of (opponent_id, is_home, difficulty) for that GW.

        A team missing from the result has a blank gameweek; a team with two
        entries has a double.
        """
        out: dict[int, list] = {}
        for fx in self.fixtures:
            if fx.get("event") != gw:
                continue
            out.setdefault(fx["team_h"], []).append(
                (fx["team_a"], True, fx["team_h_difficulty"])
            )
            out.setdefault(fx["team_a"], []).append(
                (fx["team_h"], False, fx["team_a_difficulty"])
            )
        return out

    def fixture_multiplier(self, pos: int, difficulty: int, is_home: bool) -> float:
        s = FIXTURE_SENSITIVITY[pos]
        mult = 1.0 + (3 - difficulty) * s
        mult *= 1 + (HOME_EDGE if is_home else -HOME_EDGE)
        return max(0.4, mult)

    # ---------- projection ----------

    def project(self, gws: list[int]) -> None:
        """Fill in expected points per gameweek for every player."""
        per_gw = {gw: self.team_fixtures(gw) for gw in gws}
        for p in self.players:
            for gw in gws:
                p.avail_by_gw[gw] = self.availability_in(p, gw)
                base = p.fit_per_game * p.avail_by_gw[gw]
                fxs = per_gw[gw].get(p.team, [])
                p.xpts_by_gw[gw] = sum(
                    base * self.fixture_multiplier(p.pos, d, h) for _, h, d in fxs
                )

    def horizon(self, p: Player, gws: list[int], decay: float = 0.85) -> float:
        """Total expected points over a run, discounting distant gameweeks
        because squads, form and injuries all drift."""
        return sum(p.xpts_by_gw.get(gw, 0.0) * (decay**i) for i, gw in enumerate(gws))

    def by_id(self) -> dict[int, Player]:
        return {p.id: p for p in self.players}

    def blanks_and_doubles(self, gws: list[int]) -> dict[int, dict[str, list[str]]]:
        out = {}
        for gw in gws:
            tf = self.team_fixtures(gw)
            blank = [t["short_name"] for tid, t in self.teams.items() if tid not in tf]
            double = [
                self.teams[tid]["short_name"] for tid, f in tf.items() if len(f) > 1
            ]
            if blank or double:
                out[gw] = {"blank": sorted(blank), "double": sorted(double)}
        return out
