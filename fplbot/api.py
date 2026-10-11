"""Cached client for the public Fantasy Premier League API.

Every endpoint used here is public and needs no authentication. Responses are
cached on disk so a weekly run does not hammer the API, and so a network
failure degrades to stale data instead of crashing mid-report.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://fantasy.premierleague.com/api"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
UA = "Mozilla/5.0 (compatible; fpl-assistant/1.0)"

# Bootstrap carries prices and injury news, which move daily; fixtures barely
# change. Player histories only grow after a match finishes.
TTL = {
    "bootstrap-static": 3 * 3600,
    "fixtures": 24 * 3600,
    "element-summary": 12 * 3600,
    "entry": 3 * 3600,
}


class FPLError(RuntimeError):
    pass


def _cache_path(path: str) -> Path:
    return CACHE_DIR / (path.strip("/").replace("/", "_") + ".json")


def _ttl_for(path: str) -> int:
    head = path.strip("/").split("/")[0]
    return TTL.get(head, 6 * 3600)


def get(path: str, force: bool = False) -> dict | list:
    """GET an API path, using the disk cache when it is still fresh.

    On network failure we fall back to stale cache if we have any, because a
    slightly out-of-date report beats no report.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = _cache_path(path)
    ttl = _ttl_for(path)

    if not force and fp.exists() and (time.time() - fp.stat().st_mtime) < ttl:
        return json.loads(fp.read_text())

    url = f"{BASE}/{path.strip('/')}/"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        if fp.exists():
            age_h = (time.time() - fp.stat().st_mtime) / 3600
            print(f"  ! {path} fetch failed ({exc}); using cache {age_h:.1f}h old")
            return json.loads(fp.read_text())
        raise FPLError(f"could not fetch {url}: {exc}") from exc

    fp.write_text(json.dumps(data))
    return data


def bootstrap(force: bool = False) -> dict:
    return get("bootstrap-static", force)


def fixtures(force: bool = False) -> list:
    return get("fixtures", force)


def element_summary(player_id: int, force: bool = False) -> dict:
    """Per-player detail. `history_past` holds true previous-season totals."""
    return get(f"element-summary/{player_id}", force)


def entry(entry_id: int, force: bool = False) -> dict:
    return get(f"entry/{entry_id}", force)


def entry_history(entry_id: int, force: bool = False) -> dict:
    return get(f"entry/{entry_id}/history", force)


def entry_picks(entry_id: int, gw: int, force: bool = False) -> dict:
    return get(f"entry/{entry_id}/event/{gw}/picks", force)


def league_standings(league_id: int, force: bool = False) -> dict:
    return get(f"leagues-classic/{league_id}/standings", force)


def event_live(gw: int, force: bool = False) -> dict:
    return get(f"event/{gw}/live", force)
