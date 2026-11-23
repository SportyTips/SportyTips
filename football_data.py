"""
Real football facts for SportyTips.
Uses SportAPI match history to calculate stats, probabilities and the written reasons.

What is real here:   goals scored/conceded, over-line hits, venue splits, head-to-head
                     (taken from the match results the API already returns).
What is NOT real:    corners and shots. The API results carry no such numbers, so those
                     picks keep the bookmaker's own probability and say so.
"""

import math
import os
import re
import time
import unicodedata

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
CACHE_VERSION = os.getenv("FOOTBALL_CACHE_VERSION", "v9_sportapi_reasons")

RECENT_N = int(os.getenv("RECENT_N", "10"))            # games used for recent form
MIN_DATA = int(os.getenv("MIN_DATA_GAMES", "3"))       # fewer games than this = no usable data
LEAGUE_AVG_GOALS = float(os.getenv("LEAGUE_AVG_GOALS", "1.35"))
SHRINK_K = float(os.getenv("SHRINK_K", "4"))           # pulls small samples toward the league average
MODEL_WEIGHT = float(os.getenv("MODEL_WEIGHT", "0.6")) # share of the model vs the bookmaker price

NO_DATA_TEXT = "No recent-form data was found for this match, so this pick is based on the bookmaker odds."

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
    _facts_cache[key] = (time.time() + int(ttl), value)
    return value


# ============================================================
# TEAM NORMALISATION & MATCHING (unchanged)
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
# MATCH RESULTS -> CLEAN RECORDS
# ============================================================
def _num(value):
    """Turn an API score (int, str, or {'current': 2}) into an int, or None."""
    if isinstance(value, dict):
        for key in ("current", "display", "normaltime", "fullTime", "total"):
            if value.get(key) is not None:
                return _num(value.get(key))
        return None
    try:
        if value is None or value == "": return None
        return int(float(value))
    except Exception:
        return None


def _match_scores(m):
    hs = aws = None
    for key in ("home_score", "homeScore", "home_goals"):
        if hs is None: hs = _num(m.get(key))
    for key in ("away_score", "awayScore", "away_goals"):
        if aws is None: aws = _num(m.get(key))
    score = m.get("score")
    if isinstance(score, dict):
        ft = score.get("fullTime") or score.get("fulltime") or score.get("ft") or score
        if isinstance(ft, dict):
            if hs is None: hs = _num(ft.get("home"))
            if aws is None: aws = _num(ft.get("away"))
    return hs, aws


def _side_id(m, side):
    obj = m.get("homeTeam" if side == "home" else "awayTeam")
    if isinstance(obj, dict) and obj.get("id") is not None:
        return str(obj["id"])
    for key in (("home_id", "homeTeamId") if side == "home" else ("away_id", "awayTeamId")):
        if m.get(key) is not None:
            return str(m[key])
    return None


def _match_date(m):
    for key in ("date", "utcDate", "startTime", "start_time", "kickoff", "startDate", "timestamp", "start_timestamp"):
        value = m.get(key)
        if not value: continue
        try:
            if isinstance(value, (int, float)):
                value = value / 1000 if value > 1e11 else value
                return datetime.fromtimestamp(value, timezone.utc)
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


_EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _records(matches, team_id):
    """Every finished match of one team, newest first."""
    tid = str(team_id)
    out = []
    for m in matches or []:
        if not isinstance(m, dict): continue
        hid, aid = _side_id(m, "home"), _side_id(m, "away")
        if tid not in (hid, aid): continue
        hs, aws = _match_scores(m)
        if hs is None or aws is None: continue
        is_home = hid == tid
        out.append({
            "gf": hs if is_home else aws,
            "ga": aws if is_home else hs,
            "home": is_home,
            "opp": aid if is_home else hid,
            "date": _match_date(m),
            "key": str(m.get("id") or ""),
        })
    if any(r["date"] for r in out):
        out.sort(key=lambda r: r["date"] or _EARLIEST, reverse=True)
    return out


def _block(games):
    n = len(games)
    if n == 0:
        return {"played": 0, "games": []}
    return {
        "played": n,
        "games": games,
        "scored_in": sum(1 for g in games if g["gf"] > 0),
        "conceded_in": sum(1 for g in games if g["ga"] > 0),
        "avg_scored": sum(g["gf"] for g in games) / n,
        "avg_conceded": sum(g["ga"] for g in games) / n,
        "wins": sum(1 for g in games if g["gf"] > g["ga"]),
        "draws": sum(1 for g in games if g["gf"] == g["ga"]),
        "losses": sum(1 for g in games if g["gf"] < g["ga"]),
    }


def _shrunk(avg, n):
    return (avg * n + LEAGUE_AVG_GOALS * SHRINK_K) / (n + SHRINK_K)


_results_cache = {}


def _team_results(team_id):
    hit = _results_cache.get(team_id)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        data = sd.sp_team_results(team_id) or []
    except Exception as exc:
        print(f"Team results failed for {team_id}: {exc}")
        data = []
    _results_cache[team_id] = (time.time() + FACTS_CACHE_SECONDS, data)
    return data


def _fmt_date(dt):
    return dt.strftime("%d %b %Y").lstrip("0") if dt else ""


def _head_to_head(h_all, a_all, home_id, away_id):
    """Meetings found in either team's history, shown from the home side's view."""
    seen, meetings = set(), []
    for recs, is_home_list in ((h_all, True), (a_all, False)):
        want = str(away_id) if is_home_list else str(home_id)
        for r in recs:
            if r["opp"] != want: continue
            hg, ag = (r["gf"], r["ga"]) if is_home_list else (r["ga"], r["gf"])
            key = (r["date"].date() if r["date"] else r["key"], hg, ag)
            if key in seen: continue
            seen.add(key)
            meetings.append({"hg": hg, "ag": ag, "date": r["date"]})
    if any(m["date"] for m in meetings):
        meetings.sort(key=lambda m: m["date"] or _EARLIEST, reverse=True)
    out = {"played": len(meetings), "meetings": meetings}
    if meetings:
        latest = meetings[0]
        out["latest"] = {"score": f"{latest['hg']}-{latest['ag']}", "date": _fmt_date(latest["date"])}
    return out


# ============================================================
# FACT EXTRACTION
# ============================================================
def _default_stats():
    return {"goals_scored": LEAGUE_AVG_GOALS, "goals_conceded": LEAGUE_AVG_GOALS,
            "corners": 4.5, "shots_on_target": 4.0, "total_shots": 10.0}


def get_facts(fixture):
    if not fixture: return None

    home_team, home = _fd4_team(fixture, "home")
    away_team, away = _fd4_team(fixture, "away")
    home_id, away_id = home_team.get("id"), away_team.get("id")
    fixture_id = fixture.get("id")

    if not home_id or not away_id:
        return None

    cache_key = f"facts:{CACHE_VERSION}:{home_id}:{away_id}"
    cached = _cache_get(cache_key)
    if cached: return cached

    try:
        h_all = _records(_team_results(home_id), home_id)
        a_all = _records(_team_results(away_id), away_id)

        recent_h, recent_a = h_all[:RECENT_N], a_all[:RECENT_N]
        venue_h = [r for r in h_all if r["home"]][:RECENT_N]
        venue_a = [r for r in a_all if not r["home"]][:RECENT_N]

        recent = {"home": _block(recent_h), "away": _block(recent_a)}
        venue = {"home": _block(venue_h), "away": _block(venue_a)}
        h2h = _head_to_head(h_all, a_all, home_id, away_id)

        nh, na = recent["home"]["played"], recent["away"]["played"]
        has_data = nh >= MIN_DATA and na >= MIN_DATA

        h_att = _shrunk(recent["home"].get("avg_scored", LEAGUE_AVG_GOALS), nh)
        h_def = _shrunk(recent["home"].get("avg_conceded", LEAGUE_AVG_GOALS), nh)
        a_att = _shrunk(recent["away"].get("avg_scored", LEAGUE_AVG_GOALS), na)
        a_def = _shrunk(recent["away"].get("avg_conceded", LEAGUE_AVG_GOALS), na)

        h_stats, a_stats = _default_stats(), _default_stats()
        h_stats["goals_scored"], h_stats["goals_conceded"] = h_att, h_def
        a_stats["goals_scored"], a_stats["goals_conceded"] = a_att, a_def

        facts = {
            "home": home, "away": away, "home_id": home_id, "away_id": away_id,
            "has_data": has_data,
            "stats": {
                "home": h_stats, "away": a_stats,
                "match": {
                    "corners": h_stats["corners"] + a_stats["corners"],
                    "shots_on_target": h_stats["shots_on_target"] + a_stats["shots_on_target"],
                    "total_shots": h_stats["total_shots"] + a_stats["total_shots"],
                },
            },
            "lambda": {"home": (h_att + a_def) / 2, "away": (a_att + h_def) / 2},
            "recent": recent,
            "venue": venue,
            "h2h": h2h,
            "fixture_id": fixture_id,
        }
        print(f"FACTS {home} vs {away}: results {nh}/{na}, venue {venue['home']['played']}/{venue['away']['played']}, h2h {h2h['played']}, data={has_data}")

        _cache_set(cache_key, facts, FACTS_CACHE_SECONDS)
        return facts

    except Exception as exc:
        print(f"Stats failed for {home} vs {away}, using defaults. Error: {exc}")
        return {
            "home": home, "away": away, "home_id": home_id, "away_id": away_id,
            "has_data": False,
            "stats": {
                "home": _default_stats(), "away": _default_stats(),
                "match": {"corners": 9.0, "shots_on_target": 8.0, "total_shots": 20.0},
            },
            "lambda": {"home": LEAGUE_AVG_GOALS, "away": LEAGUE_AVG_GOALS},
            "recent": {"home": {"played": 0, "games": []}, "away": {"played": 0, "games": []}},
            "venue": {"home": {"played": 0, "games": []}, "away": {"played": 0, "games": []}},
            "h2h": {"played": 0, "meetings": []},
            "fixture_id": fixture_id,
        }


# ============================================================
# PROBABILITY
# ============================================================
def _poisson(k, lam):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_prob(lam_h, lam_a, condition):
    total = 0.0
    for h in range(0, 11):
        for a in range(0, 11):
            if condition(h, a): total += _poisson(h, lam_h) * _poisson(a, lam_a)
    return min(1.0, total)


def _team_over(lam, line):
    """Chance one team scores more than `line` goals."""
    return max(0.0, 1.0 - sum(_poisson(k, lam) for k in range(0, int(math.floor(line)) + 1)))


def _candidate_text(candidate):
    return str(candidate.get("label", "")).lower() if isinstance(candidate, dict) else ""


def _kind_of(c):
    kind = str(c.get("kind") or "").lower()
    if kind: return kind
    t = _candidate_text(c)
    if "corner" in t: return "corners"
    if "shot" in t: return "shots"
    if "both teams" in t or "btts" in t: return "btts"
    if "team goals" in t or ("to score" in t and "goals" in t): return "team_goals"
    if "either half" in t: return "either_half"
    if "handicap" in t: return "handicap"
    if "1up" in t or "2up" in t: return "up"
    if " or draw" in t or "draw or" in t: return "dc"
    if "over" in t and "goals" in t: return "over"
    if "win" in t: return "win"
    return ""


def _line_of(c, default):
    try:
        if c.get("line") is not None: return float(c["line"])
    except Exception:
        pass
    m = re.search(r"(\d+(?:\.\d+)?)", _candidate_text(c).replace("1up", "").replace("2up", ""))
    return float(m.group(1)) if m else default


def _side_of_candidate(c, facts):
    side = c.get("side")
    if side in ("home", "away"): return side
    t = _candidate_text(c)
    h, a = str(facts.get("home") or "").lower(), str(facts.get("away") or "").lower()
    if h and h in t and not (a and a in t): return "home"
    if a and a in t and not (h and h in t): return "away"
    return None


def _model_p(c, facts):
    kind = _kind_of(c)
    hl, al = facts["lambda"]["home"], facts["lambda"]["away"]
    side = _side_of_candidate(c, facts)

    if kind in ("over", "over15"):
        line = _line_of(c, 1.5)
        return _poisson_prob(hl, al, lambda h, a: h + a > line)

    if kind == "btts":
        return _poisson_prob(hl, al, lambda h, a: h > 0 and a > 0)

    if kind == "team_goals" and side:
        return _team_over(hl if side == "home" else al, _line_of(c, 0.5))

    if kind in ("win", "up") and side:
        p = _poisson_prob(hl, al, (lambda h, a: h > a) if side == "home" else (lambda h, a: a > h))
        if kind == "up":
            p *= 0.88 if int(c.get("up_level") or 1) >= 2 else 0.96
        return min(0.98, p)

    if kind == "either_half" and side:
        def half(f):
            return _poisson_prob(hl * f, al * f, (lambda h, a: h > a) if side == "home" else (lambda h, a: a > h))
        return min(0.98, 1.0 - (1.0 - half(0.45)) * (1.0 - half(0.55)))

    if kind == "dc" and side:
        return _poisson_prob(hl, al, (lambda h, a: h >= a) if side == "home" else (lambda h, a: a >= h))

    if kind == "handicap" and side:
        hv = float(c.get("handicap") or 0)
        sign = 1 if side == "home" else -1
        cover = _poisson_prob(hl, al, lambda h, a: sign * (h - a) + hv > 0)
        push = _poisson_prob(hl, al, lambda h, a: sign * (h - a) + hv == 0) if hv == int(hv) else 0.0
        return min(0.98, cover + 0.5 * push)

    return None   # corners, shots, anything unknown


def adjusted_p(candidate, facts):
    if not facts: return 0.0
    try:
        market_p = float(candidate.get("market_p") or candidate.get("p") or 0.0)
    except Exception:
        market_p = 0.0

    # No usable history: keep the bookmaker's own number. Never invent one.
    if not facts.get("has_data"):
        return max(0.0, min(0.97, market_p))

    model = _model_p(candidate, facts)
    if model is None:
        return max(0.0, min(0.97, market_p))

    p = MODEL_WEIGHT * model + (1.0 - MODEL_WEIGHT) * market_p if market_p > 0 else model
    return max(0.02, min(0.97, p))


# ============================================================
# REASONING
# ============================================================
def _count(games, pred):
    return sum(1 for g in games if pred(g))


def _where(side):
    return "at home" if side == "home" else "away"


def _need(line):
    return int(math.floor(line)) + 1


def _counter(kind, facts, side, names, line):
    """One line about the biggest risk, from each team's last 5 games."""
    best = [None]

    def add(count, text, minimum):
        if count >= minimum and (best[0] is None or count > best[0][0]):
            best[0] = (count, text)

    for s in ("home", "away"):
        games = facts["recent"][s].get("games", [])[:5]
        n = len(games)
        if n < 3: continue
        name = names[s]
        blanks = _count(games, lambda g: g["gf"] == 0)
        clean = _count(games, lambda g: g["ga"] == 0)
        losses = _count(games, lambda g: g["gf"] < g["ga"])

        if kind in ("over", "over15"):
            under = _count(games, lambda g: g["gf"] + g["ga"] <= line)
            add(under, f"{name} had {under} of its last {n} games finish under {line:g}", 2)
            add(blanks, f"{name} failed to score in {blanks} of its last {n}", 2)
        elif kind == "btts":
            add(blanks, f"{name} failed to score in {blanks} of its last {n}", 1)
            add(clean, f"{name} kept a clean sheet in {clean} of its last {n}", 2)
        elif kind == "team_goals":
            need = _need(line)
            if s == side:
                short = _count(games, lambda g: g["gf"] < need)
                add(short, f"{name} scored fewer than {need} in {short} of its last {n}", 2)
            else:
                add(clean, f"{name} kept a clean sheet in {clean} of its last {n}", 2)
        elif kind == "handicap":
            hv = float(line or 0)
            if s == side:
                bad = _count(games, lambda g: g["gf"] - g["ga"] + hv <= 0)
                add(bad, f"{name} would not have covered +{hv:g} in {bad} of its last {n}", 1)
        elif s == side or side is None:
            add(losses, f"{name} lost {losses} of its last {n}", 1)
            add(blanks, f"{name} failed to score in {blanks} of its last {n}", 2)

    return f"Counter is {best[0][1]}." if best[0] else ""


def _build_reason(c, facts, h, a):
    kind = _kind_of(c)
    side = _side_of_candidate(c, facts)
    names = {"home": h, "away": a}
    rec, ven = facts["recent"], facts["venue"]
    rh, ra = rec["home"].get("games", []), rec["away"].get("games", [])
    vh, va = ven["home"].get("games", []), ven["away"].get("games", [])
    h2h = facts.get("h2h") or {}
    meetings = h2h.get("meetings", [])
    sentences = []

    if kind in ("corners", "shots"):
        return "Corner and shot numbers are not available for this match, so this pick relies on the bookmaker odds."

    if kind in ("over", "over15"):
        line = _line_of(c, 1.5)
        hit = lambda g: g["gf"] + g["ga"] > line
        s = f"{h}'s games went over {line:g} in {_count(rh, hit)}/{len(rh)} recent and {a}'s in {_count(ra, hit)}/{len(ra)}"
        if len(vh) >= MIN_DATA and len(va) >= MIN_DATA:
            s += f" ({h} {_count(vh, hit)}/{len(vh)} at home, {a} {_count(va, hit)}/{len(va)} away)"
        sentences.append(s + ".")
        if len(meetings) >= 2:
            k = sum(1 for m in meetings if m["hg"] + m["ag"] > line)
            s = f"{k} of {len(meetings)} recent meetings went over {line:g}"
            if h2h.get("latest"):
                s += f", latest {h2h['latest']['score']}" + (f" on {h2h['latest']['date']}" if h2h["latest"]["date"] else "")
            sentences.append(s + ".")
        sentences.append(_counter(kind, facts, side, names, line))

    elif kind == "btts":
        both = lambda g: g["gf"] > 0 and g["ga"] > 0
        s = f"Both teams scored in {_count(rh, both)}/{len(rh)} of {h}'s and {_count(ra, both)}/{len(ra)} of {a}'s recent games"
        if len(vh) >= MIN_DATA and len(va) >= MIN_DATA:
            s += f" ({h} {_count(vh, both)}/{len(vh)} at home, {a} {_count(va, both)}/{len(va)} away)"
        sentences.append(s + ".")
        if len(meetings) >= 2:
            k = sum(1 for m in meetings if m["hg"] > 0 and m["ag"] > 0)
            sentences.append(f"Both scored in {k} of {len(meetings)} recent meetings.")
        sentences.append(_counter(kind, facts, side, names, 0))

    elif side in ("home", "away"):
        opp = "away" if side == "home" else "home"
        team, other = names[side], names[opp]
        tg, og = rec[side].get("games", []), rec[opp].get("games", [])
        tv = (vh if side == "home" else va)
        ov = (va if side == "home" else vh)
        s_in = lambda g: g["gf"] > 0
        c_in = lambda g: g["ga"] > 0
        won = lambda g: g["gf"] > g["ga"]
        line = _line_of(c, 0.5) if kind == "team_goals" else (float(c.get("handicap") or 0) if kind == "handicap" else 0)

        if kind in ("win", "up"):
            s = f"{team} won {_count(tg, won)}/{len(tg)} recent"
            if len(tv) >= MIN_DATA: s += f" ({_count(tv, won)}/{len(tv)} {_where(side)})"
            sentences.append(s + ".")
        elif kind == "dc":
            unb = lambda g: g["gf"] >= g["ga"]
            s = f"{team} avoided defeat in {_count(tg, unb)}/{len(tg)} recent"
            if len(tv) >= MIN_DATA: s += f" ({_count(tv, unb)}/{len(tv)} {_where(side)})"
            sentences.append(s + ".")
        elif kind == "handicap":
            hv = float(c.get("handicap") or 0)
            cov = lambda g: g["gf"] - g["ga"] + hv > 0
            sentences.append(f"{team} would have covered +{hv:g} in {_count(tg, cov)}/{len(tg)} recent games.")

        if kind == "team_goals":
            need = _need(line)
            hitn = lambda g: g["gf"] >= need
            s = f"{team} scored {need}+ in {_count(tg, hitn)}/{len(tg)} recent"
            if len(tv) >= MIN_DATA: s += f" ({_count(tv, hitn)}/{len(tv)} {_where(side)})"
            s += f", and {other} conceded {need}+ in {_count(og, lambda g: g['ga'] >= need)}/{len(og)}"
        else:
            s = f"{team} scored in {_count(tg, s_in)}/{len(tg)} recent"
            if len(tv) >= MIN_DATA: s += f" ({_count(tv, s_in)}/{len(tv)} {_where(side)})"
            s += f", while {other} conceded in {_count(og, c_in)}/{len(og)}"
            if len(ov) >= MIN_DATA: s += f" ({_count(ov, c_in)}/{len(ov)} {_where(opp)})"
        sentences.append(s + ".")

        if len(meetings) >= 2:
            if side == "home":
                w = sum(1 for m in meetings if m["hg"] > m["ag"])
            else:
                w = sum(1 for m in meetings if m["ag"] > m["hg"])
            s = f"{team} won {w} of {len(meetings)} recent meetings"
            if h2h.get("latest"):
                s += f", latest {h2h['latest']['score']}" + (f" on {h2h['latest']['date']}" if h2h["latest"]["date"] else "")
            sentences.append(s + ".")
        sentences.append(_counter(kind, facts, side, names, line))

    return " ".join(x for x in sentences if x).strip()


def reason_for(candidate, facts, home=None, away=None):
    if not facts or not facts.get("has_data"):
        return NO_DATA_TEXT
    h, a = facts.get("home") or home or "Home", facts.get("away") or away or "Away"
    try:
        text = _build_reason(candidate, facts, h, a)
        if text: return text
    except Exception as exc:
        print(f"Reason failed for {h} vs {a}: {type(exc).__name__}: {exc}")
    hs, as_ = facts["stats"]["home"], facts["stats"]["away"]
    return (f"{h} scores {hs['goals_scored']:.2f} per game and concedes {hs['goals_conceded']:.2f}. "
            f"{a} scores {as_['goals_scored']:.2f} and concedes {as_['goals_conceded']:.2f}.")


# ============================================================
# DIAGNOSTICS
# ============================================================
def diag_summary():
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