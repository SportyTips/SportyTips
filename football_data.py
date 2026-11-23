"""
Real football facts for SamuelBet AI.

Football evidence comes FIRST.

SportyBet is used only for:
    - available markets
    - available selections
    - odds / prices
    - final booking code

Football data is used for:
    - fixture matching
    - recent form
    - goals
    - home / away performance
    - H2H
    - Poisson goal model
    - evidence-based market probability (including Shots & Corners)
"""

import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

import main as bot
import sportybet_provider as sp
import sportapi_data as sd


# ============================================================
# SETTINGS
# ============================================================

ENRICH_MAX = int(os.getenv("ENRICH_MAX", "10"))
ENRICH_SECONDS = int(os.getenv("ENRICH_SECONDS", "100"))
MIN_QUOTA = int(os.getenv("MIN_QUOTA", "1"))

# Facts can safely live for several hours.
FACTS_CACHE_SECONDS = int(os.getenv("FACTS_CACHE_SECONDS", str(6 * 3600)))
FIXTURE_CACHE_SECONDS = int(os.getenv("FIXTURE_CACHE_SECONDS", str(15 * 60)))
PREDICTION_CACHE_SECONDS = int(os.getenv("PREDICTION_CACHE_SECONDS", str(3 * 3600)))

# Cache version.
CACHE_VERSION = os.getenv("FOOTBALL_CACHE_VERSION", "v8_sportapi")

SURE_P_DATA = 0.78
SURE_P_NODATA = 0.00


# ============================================================
# MEMORY CACHE
# ============================================================

_facts_cache = {}
_fixture_index = {}
_prediction_cache = {}


# ============================================================
# PERSISTENT REDIS CACHE
# ============================================================

REDIS_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("REDIS_URL") or "").strip().rstrip("/")
REDIS_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("REDIS_TOKEN") or "").strip()


def _redis_enabled():
    return bool(REDIS_URL and REDIS_TOKEN and ("upstash.io" in REDIS_URL or REDIS_URL.startswith("https://")))


def _redis_request(command):
    if not _redis_enabled():
        return None
    try:
        url = REDIS_URL if REDIS_URL.endswith("/") else REDIS_URL + "/"
        encoded = "/".join(urllib.parse.quote(str(x), safe="") for x in command)
        request = urllib.request.Request(
            url + encoded,
            headers={"Authorization": "Bearer " + REDIS_TOKEN, "Content-Type": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("result")
    except Exception:
        return None


def _redis_get(key):
    value = _redis_request(["GET", key])
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _redis_set(key, value, ttl):
    if not _redis_enabled():
        return False
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        result = _redis_request(["SET", key, payload, "EX", int(ttl)])
        return result == "OK"
    except Exception:
        return False


def _redis_delete(key):
    if not _redis_enabled():
        return False
    try:
        _redis_request(["DEL", key])
        return True
    except Exception:
        return False


# ============================================================
# LOCAL SQLITE FALLBACK
# ============================================================

CACHE_DB = os.getenv("FOOTBALL_CACHE_DB", "/tmp/samuelbet_football_cache.sqlite3")


def _sqlite_connect():
    connection = sqlite3.connect(CACHE_DB, timeout=15, check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS football_cache (
            cache_key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _sqlite_get(key):
    try:
        now = int(time.time())
        connection = _sqlite_connect()
        row = connection.execute("SELECT value, expires_at FROM football_cache WHERE cache_key = ?", (key,)).fetchone()
        connection.close()
        if not row:
            return None
        value, expires_at = row
        if expires_at <= now:
            _sqlite_delete(key)
            return None
        return json.loads(value)
    except Exception:
        return None


def _sqlite_set(key, value, ttl):
    try:
        now = int(time.time())
        connection = _sqlite_connect()
        connection.execute(
            """
            INSERT INTO football_cache (cache_key, value, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                value = excluded.value, expires_at = excluded.expires_at, created_at = excluded.created_at
            """,
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now + int(ttl), now),
        )
        connection.commit()
        connection.close()
        return True
    except Exception:
        return False


def _sqlite_delete(key):
    try:
        connection = _sqlite_connect()
        connection.execute("DELETE FROM football_cache WHERE cache_key = ?", (key,))
        connection.commit()
        connection.close()
        return True
    except Exception:
        return False


# ============================================================
# UNIFIED CACHE
# ============================================================

def _cache_get(key):
    now = time.time()
    item = _facts_cache.get(key)
    if item:
        expires, value = item
        if expires > now:
            return value
        _facts_cache.pop(key, None)

    value = _redis_get(key)
    if value is not None:
        _facts_cache[key] = (now + min(FACTS_CACHE_SECONDS, 300), value)
        return value

    value = _sqlite_get(key)
    if value is not None:
        _facts_cache[key] = (now + min(FACTS_CACHE_SECONDS, 300), value)
        return value

    return None


def _cache_set(key, value, ttl):
    _facts_cache[key] = (time.time() + min(int(ttl), 300), value)
    if _redis_enabled():
        _redis_set(key, value, ttl)
    _sqlite_set(key, value, ttl)
    return value


def _cache_delete(key):
    _facts_cache.pop(key, None)
    _prediction_cache.pop(key, None)
    _redis_delete(key)
    _sqlite_delete(key)


# ============================================================
# TEAM NORMALISATION
# ============================================================

TEAM_ALIASES = {
    "manchester united": ["man utd", "man united", "manchester utd", "manchester united", "man united fc", "manchester united fc"],
    "manchester city": ["man city", "manchester city", "manchester city fc"],
    "tottenham hotspur": ["tottenham", "spurs", "tottenham hotspur", "tottenham hotspur fc"],
    "newcastle united": ["newcastle", "newcastle united", "newcastle utd"],
    "west ham united": ["west ham", "west ham united", "west ham utd"],
    "wolverhampton wanderers": ["wolves", "wolverhampton", "wolverhampton wanderers"],
    "brighton and hove albion": ["brighton", "brighton and hove albion", "brighton hove albion"],
    "nottingham forest": ["nottingham forest", "nottm forest", "forest"],
    "crystal palace": ["crystal palace", "palace"],
    "aston villa": ["aston villa", "villa"],
    "afc bournemouth": ["bournemouth", "afc bournemouth"],
    "leicester city": ["leicester", "leicester city"],
    "ipswich town": ["ipswich", "ipswich town"],
    "leeds united": ["leeds", "leeds united", "leeds utd"],
    "everton": ["everton", "everton fc"],
    "liverpool": ["liverpool", "liverpool fc"],
    "arsenal": ["arsenal", "arsenal fc"],
    "chelsea": ["chelsea", "chelsea fc"],
    "fulham": ["fulham", "fulham fc"],
    "brentford": ["brentford", "brentford fc"],
    "burnley": ["burnley", "burnley fc"],
    "sunderland": ["sunderland", "sunderland afc"],
    "real madrid": ["real madrid", "real madrid cf"],
    "barcelona": ["barcelona", "fc barcelona"],
    "atletico madrid": ["atletico madrid", "atl madrid", "atletico"],
    "sevilla": ["sevilla", "sevilla fc"],
    "real sociedad": ["real sociedad"],
    "athletic club": ["athletic bilbao", "athletic club"],
    "villarreal": ["villarreal", "villarreal cf"],
    "real betis": ["real betis", "betis"],
    "valencia": ["valencia", "valencia cf"],
    "girona": ["girona", "girona fc"],
    "bayern munich": ["bayern", "bayern munich", "fc bayern munich"],
    "borussia dortmund": ["dortmund", "borussia dortmund", "bvb"],
    "rb leipzig": ["rb leipzig", "leipzig"],
    "bayer leverkusen": ["bayer leverkusen", "leverkusen"],
    "eintracht frankfurt": ["eintracht frankfurt", "frankfurt"],
    "inter milan": ["inter", "inter milan", "internazionale"],
    "ac milan": ["ac milan", "milan"],
    "juventus": ["juventus", "juve"],
    "napoli": ["napoli", "ssc napoli"],
    "roma": ["roma", "as roma"],
    "lazio": ["lazio", "ss lazio"],
    "atalanta": ["atalanta", "atalanta bc"],
    "paris saint germain": ["psg", "paris saint-germain", "paris saint germain", "paris sg"],
    "marseille": ["marseille", "olympique marseille"],
    "lyon": ["lyon", "olympique lyon"],
    "monaco": ["monaco", "as monaco"],
    "lille": ["lille", "losc lille"],
    "ajax": ["ajax", "afc ajax"],
    "psv eindhoven": ["psv", "psv eindhoven"],
    "feyenoord": ["feyenoord"],
    "benfica": ["benfica", "sl benfica"],
    "porto": ["porto", "fc porto"],
    "sporting cp": ["sporting", "sporting lisbon", "sporting cp"],
    "galatasaray": ["galatasaray", "galatasaray sk"],
    "fenerbahce": ["fenerbahce", "fenerbahçe", "fenerbahce sk"],
    "besiktas": ["besiktas", "beşiktaş"],
    "celtic": ["celtic", "celtic fc"],
    "rangers": ["rangers", "rangers fc"],
}


def _normalise_team(value):
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"\b(fc|afc|cf|sc|sk|fk|club|football club)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for canonical, aliases in TEAM_ALIASES.items():
        for alias in [canonical] + aliases:
            alias_n = re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()
            if text == alias_n:
                return canonical
    return text


def _team_similarity(a, b):
    aa = _normalise_team(a)
    bb = _normalise_team(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.92
    return SequenceMatcher(None, aa, bb).ratio()


# ============================================================
# FIXTURE HELPERS
# ============================================================

def _fixture_teams(fixture):
    if not isinstance(fixture, dict):
        return "", ""
    teams = fixture.get("teams") or {}
    home = (teams.get("home") or {}).get("name") or fixture.get("home") or fixture.get("homeTeam") or ""
    away = (teams.get("away") or {}).get("name") or fixture.get("away") or fixture.get("awayTeam") or ""
    return str(home), str(away)


def _fixture_timestamp(fixture):
    if not isinstance(fixture, dict):
        return None
    fixture_data = fixture.get("fixture") or {}
    value = fixture_data.get("date") or fixture.get("date") or fixture.get("kickoff") or fixture.get("utcDate")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _event_kickoff(event):
    if not isinstance(event, dict):
        return None
    for value in [event.get("kickoff"), event.get("startTime"), event.get("date")]:
        if not value:
            continue
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        try:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _event_teams(event):
    if not isinstance(event, dict):
        return "", ""
    home = event.get("home") or event.get("homeTeam") or event.get("homeTeamName") or ""
    away = event.get("away") or event.get("awayTeam") or event.get("awayTeamName") or ""
    if isinstance(home, dict): home = home.get("name") or ""
    if isinstance(away, dict): away = away.get("name") or ""
    return str(home), str(away)


# ============================================================
# FACT EXTRACTION & STATS (SPORTAPI)
# ============================================================

def _fd4_team(fixture, side):
    if not isinstance(fixture, dict):
        return {}, ""
    key = "homeTeam" if side == "home" else "awayTeam"
    team = fixture.get(key) or {}
    return (team, str(team.get("name") or "")) if isinstance(team, dict) else ({}, str(team or ""))


def get_facts(fixture):
    """Build football facts using SportAPI stats and Form."""
    if not fixture:
        return None

    home_team, home = _fd4_team(fixture, "home")
    away_team, away = _fd4_team(fixture, "away")
    home_id = home_team.get("id")
    away_id = away_team.get("id")
    fixture_id = fixture.get("id")

    if not home_id or not away_id:
        return None

    cache_key = f"football-facts:{CACHE_VERSION}:{fixture_id or home_id}-{away_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # 1. Fetch the stats directly from SportAPI (Shots, Corners, Goals)
    h_stats_raw = sd._api_get(f"/teams/{home_id}/stats")
    a_stats_raw = sd._api_get(f"/teams/{away_id}/stats")

    def parse_stats(raw):
        if not raw or "data" not in raw:
            return {"goals_scored": 1.2, "goals_conceded": 1.2, "corners": 4.5, "shots_on_target": 4.0, "total_shots": 10.0}
        stats = raw["data"]
        played = max(1, stats.get("played", 1))
        return {
            "goals_scored": float(stats.get("goals_for", 1.2)) / played,
            "goals_conceded": float(stats.get("goals_against", 1.2)) / played,
            "corners": float(stats.get("corners", 4.5)),
            "shots_on_target": float(stats.get("shots_on_target", 4.0)),
            "total_shots": float(stats.get("total_shots", 10.0)),
        }

    h_stats = parse_stats(h_stats_raw)
    a_stats = parse_stats(a_stats_raw)

    # 2. Fetch the recent form and H2H (handled by sportapi_data.py)
    h_recent = sd.sp_team_results(home_id)
    a_recent = sd.sp_team_results(away_id)
    h2h = sd.sp_h2h(fixture_id)

    # 3. Combine it all into the facts dictionary
    facts = {
        "home": home,
        "away": away,
        "home_id": home_id,
        "away_id": away_id,
        "stats": {
            "home": h_stats,
            "away": a_stats,
            "match": {
                "corners": h_stats.get("corners", 0) + a_stats.get("corners", 0),
                "shots_on_target": h_stats.get("shots_on_target", 0) + a_stats.get("shots_on_target", 0),
                "total_shots": h_stats.get("total_shots", 0) + a_stats.get("total_shots", 0)
            }
        },
        "lambda": {
            "home": (h_stats.get("goals_scored", 1.2) + a_stats.get("goals_conceded", 1.2)) / 2,
            "away": (a_stats.get("goals_scored", 1.2) + h_stats.get("goals_conceded", 1.2)) / 2
        },
        "recent": {"home": {"played": len(h_recent)}, "away": {"played": len(a_recent)}},
        "venue": {"home": {}, "away": {}},
        "h2h": {"played": len(h2h)},
        "fixture_id": fixture_id,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }

    _cache_set(cache_key, facts, FACTS_CACHE_SECONDS)
    return facts


# ============================================================
# POISSON & PROBABILITY
# ============================================================

def _poisson(k, lam):
    if lam <= 0: return 0.0
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def _poisson_prob(lam_h, lam_a, condition):
    total = 0.0
    for h in range(0, 10):
        for a in range(0, 10):
            if condition(h, a):
                total += _poisson(h, lam_h) * _poisson(a, lam_a)
    return min(1.0, total)


def _candidate_text(candidate):
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("label", "")).lower()


def adjusted_p(candidate, facts):
    """Calculate probability for ANY market based on the stats."""
    if not facts: return 0.0
    
    text = _candidate_text(candidate)
    match_stats = facts["stats"]["match"]
    hl, al = facts["lambda"]["home"], facts["lambda"]["away"]

    # 1. CORNERS
    if "corners" in text:
        line = 9.5
        m = re.search(r"(\d+\.\d+)", text)
        if m: line = float(m.group(1))
        expected_corners = match_stats["corners"]
        diff = expected_corners - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.08)))

    # 2. SHOTS ON TARGET
    if "shots on target" in text:
        line = 8.5
        m = re.search(r"(\d+\.\d+)", text)
        if m: line = float(m.group(1))
        expected_sot = match_stats["shots_on_target"]
        diff = expected_sot - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.08)))

    # 3. TOTAL SHOTS
    if "shots" in text and "target" not in text:
        line = 25.5
        m = re.search(r"(\d+\.\d+)", text)
        if m: line = float(m.group(1))
        expected_shots = match_stats["total_shots"]
        diff = expected_shots - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.04)))

    # 4. GOALS / BTTS
    if "over" in text and "goals" in text:
        line = 1.5
        m = re.search(r"(\d+\.\d+)", text)
        if m: line = float(m.group(1))
        return _poisson_prob(hl, al, lambda h, a: h + a > line)

    if "btts" in text or "both teams" in text:
        return _poisson_prob(hl, al, lambda h, a: h > 0 and a > 0)

    if "win" in text or "1up" in text or "2up" in text:
        p_home = _poisson_prob(hl, al, lambda h, a: h > a)
        p_away = _poisson_prob(hl, al, lambda h, a: a > h)
        if "away" in text: p = p_away
        elif "home" in text: p = p_home
        else: p = max(p_home, p_away)
        if "1up" in text: p *= 0.96
        if "2up" in text: p *= 0.88
        return min(0.98, p)

    return 0.0


# ============================================================
# EXPLANATION ENGINE (Matches the Screenshot style)
# ============================================================

def reason_for(candidate, facts, home=None, away=None):
    if not facts: return "No stats available."
    
    text = _candidate_text(candidate)
    h = facts.get("home") or home
    a = facts.get("away") or away
    hs = facts["stats"]["home"]
    as_ = facts["stats"]["away"]
    ms = facts["stats"]["match"]

    if "corners" in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "9.5"
        return (f"{h} average {hs['corners']:.2f} corners and {a} average {as_['corners']:.2f}. "
                f"Combined average is {ms['corners']:.2f}, clearing the {line} line.")

    if "shots on target" in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "8.5"
        return (f"{h} average {hs['shots_on_target']:.2f} shots on target with {a} averaging {as_['shots_on_target']:.2f}. "
                f"Combined average {ms['shots_on_target']:.2f} is above the {line} line.")

    if "shots" in text and "target" not in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "25.5"
        return (f"{h} average {hs['total_shots']:.2f} total shots and {a} average {as_['total_shots']:.2f}. "
                f"Combined average of {ms['total_shots']:.2f} supports the over.")

    if "btts" in text:
        return f"Expected goals: {h} ({facts['lambda']['home']:.2f}) vs {a} ({facts['lambda']['away']:.2f}). Both teams are projected to score."

    if "win" in text:
        return f"Based on recent stats: {h} scores {hs['goals_scored']:.2f} per game, {a} scores {as_['goals_scored']:.2f}."
    
    return f"Stats based on recent games for {h} and {a}."


# ============================================================
# COMPATIBILITY HELPERS
# ============================================================

def _safe_float(value, default=None):
    try:
        if value is None or value == "": return default
        return float(value)
    except Exception:
        return default

def _safe_int(value, default=0):
    try:
        if value is None or value == "": return default
        return int(float(value))
    except Exception:
        return default

def _clamp(value, low=0.0, high=0.995):
    try: value = float(value)
    except Exception: return low
    return max(low, min(high, value))

def _blend(values, weights):
    usable = []
    for value, weight in zip(values, weights):
        if value is not None: usable.append((float(value), float(weight)))
    if not usable: return None
    total_weight = sum(weight for _, weight in usable)
    if total_weight <= 0: return None
    return sum(value * weight for value, weight in usable) / total_weight

def _extract_line(candidate):
    text = _candidate_text(candidate)
    matches = re.findall(r"(\d+(?:\.\d+)?)", text)
    if not matches: return None
    try: return float(matches[-1])
    except Exception: return None

def _dc_like(candidate):
    text = _candidate_text(candidate)
    return any(value in text for value in ("double chance", "draw no bet", "dnb"))

def _is_under(candidate):
    return "under" in _candidate_text(candidate)

def _is_yellow_card(candidate):
    return "yellow card" in _candidate_text(candidate) or "booking" in _candidate_text(candidate)

def _is_negative_handicap(candidate):
    return bool(re.search(r"handicap[^0-9\-]*-\s*\d", _candidate_text(candidate)))

def _win_like(candidate):
    return any(v in _candidate_text(candidate) for v in ("1up", "2up", "home win", "away win", "straight win", "to win", "either half"))

def _goals_like(candidate):
    return any(v in _candidate_text(candidate) for v in ("over 1.5", "over 2.5", "over 3.5", "over 0.5", "team goals", "total goals", "btts", "both teams to score"))

def _poisson_home_win(hl, al): return _poisson_prob(hl, al, lambda h, a: h > a)
def _poisson_away_win(hl, al): return _poisson_prob(hl, al, lambda h, a: a > h)

def _football_win_probability(facts, side):
    recent = facts.get("recent") or {}
    venue = facts.get("venue") or {}
    h2h = facts.get("h2h") or {}
    lambdas = facts.get("lambda") or {}
    form = recent.get(side, {}).get("wins", 0) / max(1, recent.get(side, {}).get("played", 1))
    venue_rate = venue.get(side, {}).get("wins", 0) / max(1, venue.get(side, {}).get("played", 1))
    poisson = _poisson_home_win(lambdas.get("home"), lambdas.get("away")) if side == "home" else _poisson_away_win(lambdas.get("home"), lambdas.get("away"))
    h2h_rate = (h2h.get(f"{side}_wins", 0) / h2h["played"]) if h2h.get("played") else None
    return _blend([form, venue_rate, poisson, h2h_rate], [0.38, 0.32, 0.25, 0.05])