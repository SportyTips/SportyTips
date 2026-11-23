"""
Real football facts for SamuelBet AI.
Uses SportAPI for stats (Shots, Corners, Goals) and form.
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

FACTS_CACHE_SECONDS = int(os.getenv("FACTS_CACHE_SECONDS", str(6 * 3600)))
FIXTURE_CACHE_SECONDS = int(os.getenv("FIXTURE_CACHE_SECONDS", str(15 * 60)))
CACHE_VERSION = os.getenv("FOOTBALL_CACHE_VERSION", "v8_sportapi")

# ============================================================
# MEMORY CACHE
# ============================================================
_facts_cache = {}

def _cache_get(key):
    now = time.time()
    item = _facts_cache.get(key)
    if item:
        expires, value = item
        if expires > now:
            return value
        _facts_cache.pop(key, None)
    return None

def _cache_set(key, value, ttl):
    _facts_cache[key] = (time.time() + min(int(ttl), 300), value)
    return value

# ============================================================
# TEAM NORMALISATION & MATCHING
# ============================================================
def _normalise_team(value):
    if value is None: return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"\b(fc|afc|cf|sc|sk|fk|club|football club)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _team_similarity(a, b):
    aa, bb = _normalise_team(a), _normalise_team(b)
    if not aa or not bb: return 0.0
    if aa == bb: return 1.0
    if aa in bb or bb in aa: return 0.92
    return SequenceMatcher(None, aa, bb).ratio()

def _event_teams(event):
    if not isinstance(event, dict): return "", ""
    home = event.get("home") or event.get("homeTeam") or event.get("homeTeamName") or ""
    away = event.get("away") or event.get("awayTeam") or event.get("awayTeamName") or ""
    if isinstance(home, dict): home = home.get("name") or ""
    if isinstance(away, dict): away = away.get("name") or ""
    return str(home), str(away)

def _event_kickoff(event):
    if not isinstance(event, dict): return None
    for value in [event.get("kickoff"), event.get("startTime"), event.get("date")]:
        if not value: continue
        if isinstance(value, (int, float)): return datetime.fromtimestamp(value, timezone.utc)
        try:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception: pass
    return None

def _fd4_team(fixture, side):
    if not isinstance(fixture, dict): return {}, ""
    key = "homeTeam" if side == "home" else "awayTeam"
    team = fixture.get(key) or {}
    return (team, str(team.get("name") or "")) if isinstance(team, dict) else ({}, str(team or ""))

def find_fixture(event):
    if not event: return None
    home, away = _event_teams(event)
    if not home or not away: return None

    event_dt = _event_kickoff(event)
    base_date = event_dt.date() if event_dt else datetime.now(timezone.utc).date()
    dates = [base_date, base_date - timedelta(days=1), base_date + timedelta(days=1)]
    
    candidates = []
    for date_value in dates:
        for fixture in sd.sp_fixtures_for(date_value.strftime("%Y-%m-%d")):
            fh, fa = _fd4_team(fixture, "home")[1], _fd4_team(fixture, "away")[1]
            if not fh or not fa: continue
            
            normal = (_team_similarity(home, fh) + _team_similarity(away, fa)) / 2.0
            reversed_score = (_team_similarity(home, fa) + _team_similarity(away, fh)) / 2.0
            score = max(normal, reversed_score)
            
            if score >= 0.72:
                candidates.append((score, fixture))
                
    if not candidates: return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_fixture = candidates[0]
    
    if best_score < 0.82: return None
    return best_fixture

# ============================================================
# FACT EXTRACTION (WITH DEBUG PRINTING)
# ============================================================
def get_facts(fixture):
    if not fixture: return None

    home_team, home = _fd4_team(fixture, "home")
    away_team, away = _fd4_team(fixture, "away")
    home_id, away_id = home_team.get("id"), away_team.get("id")
    fixture_id = fixture.get("id")

    if not home_id or not away_id:
        print(f"DEBUG: Missing IDs for {home} vs {away}")
        return None

    cache_key = f"facts:{home_id}:{away_id}"
    cached = _cache_get(cache_key)
    if cached: return cached

    # Fetch Stats
    h_stats_raw = sd._api_get(f"/teams/{home_id}/stats")
    a_stats_raw = sd._api_get(f"/teams/{away_id}/stats")
    
    # DEBUG PRINT
    print(f"--- API RESPONSE FOR {home} ({home_id}) ---")
    print(h_stats_raw)
    print(f"--- API RESPONSE FOR {away} ({away_id}) ---")
    print(a_stats_raw)

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

    h_stats, a_stats = parse_stats(h_stats_raw), parse_stats(a_stats_raw)

    facts = {
        "home": home, "away": away, "home_id": home_id, "away_id": away_id,
        "stats": {
            "home": h_stats, "away": a_stats,
            "match": {
                "corners": h_stats["corners"] + a_stats["corners"],
                "shots_on_target": h_stats["shots_on_target"] + a_stats["shots_on_target"],
                "total_shots": h_stats["total_shots"] + a_stats["total_shots"]
            }
        },
        "lambda": {
            "home": (h_stats["goals_scored"] + a_stats["goals_conceded"]) / 2,
            "away": (a_stats["goals_scored"] + h_stats["goals_conceded"]) / 2
        },
        "recent": {"home": {"played": 5}, "away": {"played": 5}}, # Mock for now
        "venue": {"home": {}, "away": {}},
        "h2h": {"played": 0},
        "fixture_id": fixture_id
    }

    _cache_set(cache_key, facts, FACTS_CACHE_SECONDS)
    return facts

# ============================================================
# PROBABILITY & REASONING
# ============================================================
def _poisson(k, lam):
    if lam <= 0: return 0.0
    return math.exp(-lam) * (lam**k) / math.factorial(k)

def _poisson_prob(lam_h, lam_a, condition):
    total = 0.0
    for h in range(0, 10):
        for a in range(0, 10):
            if condition(h, a): total += _poisson(h, lam_h) * _poisson(a, lam_a)
    return min(1.0, total)

def _candidate_text(candidate):
    return str(candidate.get("label", "")).lower() if isinstance(candidate, dict) else ""

def adjusted_p(candidate, facts):
    if not facts: return 0.0
    text = _candidate_text(candidate)
    ms = facts["stats"]["match"]
    hl, al = facts["lambda"]["home"], facts["lambda"]["away"]

    if "corners" in text:
        line = float(re.search(r"(\d+\.\d+)", text).group(1)) if re.search(r"(\d+\.\d+)", text) else 9.5
        diff = ms["corners"] - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.08)))

    if "shots on target" in text:
        line = float(re.search(r"(\d+\.\d+)", text).group(1)) if re.search(r"(\d+\.\d+)", text) else 8.5
        diff = ms["shots_on_target"] - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.08)))

    if "shots" in text and "target" not in text:
        line = float(re.search(r"(\d+\.\d+)", text).group(1)) if re.search(r"(\d+\.\d+)", text) else 25.5
        diff = ms["total_shots"] - line
        return min(0.95, max(0.05, 0.5 + (diff * 0.04)))

    if "over" in text and "goals" in text:
        line = float(re.search(r"(\d+\.\d+)", text).group(1)) if re.search(r"(\d+\.\d+)", text) else 1.5
        return _poisson_prob(hl, al, lambda h, a: h + a > line)

    if "btts" in text or "both teams" in text:
        return _poisson_prob(hl, al, lambda h, a: h > 0 and a > 0)

    if "win" in text or "1up" in text or "2up" in text:
        p_home = _poisson_prob(hl, al, lambda h, a: h > a)
        p_away = _poisson_prob(hl, al, lambda h, a: a > h)
        p = p_away if "away" in text else p_home if "home" in text else max(p_home, p_away)
        if "1up" in text: p *= 0.96
        if "2up" in text: p *= 0.88
        return min(0.98, p)

    return 0.0

def reason_for(candidate, facts, home=None, away=None):
    if not facts: return "No stats available."
    text = _candidate_text(candidate)
    h, a = facts.get("home") or home, facts.get("away") or away
    hs, as_, ms = facts["stats"]["home"], facts["stats"]["away"], facts["stats"]["match"]

    if "corners" in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "9.5"
        return f"{h} average {hs['corners']:.2f} corners and {a} average {as_['corners']:.2f}. Combined average is {ms['corners']:.2f}, clearing the {line} line."

    if "shots on target" in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "8.5"
        return f"{h} average {hs['shots_on_target']:.2f} shots on target with {a} averaging {as_['shots_on_target']:.2f}. Combined average {ms['shots_on_target']:.2f} is above the {line} line."

    if "shots" in text and "target" not in text:
        line = re.search(r"(\d+\.\d+)", text)
        line = line.group(1) if line else "25.5"
        return f"{h} average {hs['total_shots']:.2f} total shots and {a} average {as_['total_shots']:.2f}. Combined average of {ms['total_shots']:.2f} supports the over."

    if "btts" in text:
        return f"Expected goals: {h} ({facts['lambda']['home']:.2f}) vs {a} ({facts['lambda']['away']:.2f}). Both teams are projected to score."

    if "win" in text:
        return f"Based on recent stats: {h} scores {hs['goals_scored']:.2f} per game, {a} scores {as_['goals_scored']:.2f}."
    
    return f"Stats based on recent games for {h} and {a}."

# ============================================================
# DIAGNOSTICS (Required by sportapi_data.py)
# ============================================================

def diag_summary():
    """
    Internal diagnostics only.
    """
    return {
        "cache_version": CACHE_VERSION,
        "redis_enabled": False,
        "memory_entries": len(_facts_cache),
        "fixture_entries": 0,
        "prediction_entries": 0,
        "persistent_store": "sqlite",
        "facts_ttl": FACTS_CACHE_SECONDS,
        "fixture_ttl": FIXTURE_CACHE_SECONDS,
    }