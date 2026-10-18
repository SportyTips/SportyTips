"""
API-Football evidence + Neon (Postgres) persistent cache for SportyTips.

One API-Football call per match ("predictions") returns: win percentages,
last-5 / season form, home & away record, goals, and head-to-head.
Everything is saved in Neon, so a match is only fetched once.

Render env vars:
  API_FOOTBALL_KEY   key from api-football.com (api-sports.io)
  DATABASE_URL       your existing Neon string (already set)
  APIF_DAILY_LIMIT   optional hard stop per day (default 90, free plan = 100)

Uses psycopg2, which app.py already uses.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import psycopg2
except Exception:
    psycopg2 = None

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip().strip("\"'")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

APIF_KEY = (os.getenv("API_FOOTBALL_KEY") or os.getenv("APIF_KEY") or "").strip().strip("\"'")
APIF_DAILY_LIMIT = int(os.getenv("APIF_DAILY_LIMIT", "90"))
APIF_BASE = "https://v3.football.api-sports.io"

_table_ready = False


# ------------------------------------------------------------
# NEON CACHE (own table: football_cache; never touches users/payments)
# ------------------------------------------------------------

def neon_enabled():
    return bool(DATABASE_URL.startswith("postgresql://") and psycopg2)


def _run(sql, params=(), fetch=False):
    global _table_ready
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=8)
    try:
        with conn:
            with conn.cursor() as cur:
                if not _table_ready:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS football_cache ("
                        "cache_key TEXT PRIMARY KEY, "
                        "value JSONB NOT NULL, "
                        "expires_at TIMESTAMPTZ NOT NULL)"
                    )
                    _table_ready = True
                cur.execute(sql, params)
                return cur.fetchone() if fetch else None
    finally:
        conn.close()


def neon_get(key):
    if not neon_enabled():
        return None
    try:
        row = _run(
            "SELECT value FROM football_cache WHERE cache_key = %s AND expires_at > now()",
            (key,),
            fetch=True,
        )
        return row[0] if row else None
    except Exception as exc:
        print("Neon cache read failed:", type(exc).__name__)
        return None


def neon_set(key, value, ttl):
    if not neon_enabled():
        return False
    try:
        _run(
            "INSERT INTO football_cache (cache_key, value, expires_at) "
            "VALUES (%s, %s::jsonb, now() + (%s || ' seconds')::interval) "
            "ON CONFLICT (cache_key) DO UPDATE "
            "SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
            (key, json.dumps(value, ensure_ascii=False), str(int(ttl))),
        )
        return True
    except Exception as exc:
        print("Neon cache write failed:", type(exc).__name__)
        return False


def neon_purge_expired():
    """Optional: run now and then to keep the table small."""
    if neon_enabled():
        try:
            _run("DELETE FROM football_cache WHERE expires_at < now()")
        except Exception:
            pass


# ------------------------------------------------------------
# API-FOOTBALL REQUESTS (daily cap + per-minute throttle)
# ------------------------------------------------------------

_calls = []


def _throttle():
    now = time.time()
    while _calls and now - _calls[0] > 60:
        _calls.pop(0)
    if len(_calls) >= 9:  # free plan: 10 per minute
        time.sleep(max(0.0, 60 - (now - _calls[0]) + 0.5))
    _calls.append(time.time())


def _quota_ok():
    key = "apif-count:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = neon_get(key) or 0
    if used >= APIF_DAILY_LIMIT:
        return False
    neon_set(key, used + 1, 2 * 86400)
    return True


def _apif(endpoint, params):
    if not APIF_KEY or not _quota_ok():
        return None
    _throttle()
    try:
        url = APIF_BASE + "/" + endpoint + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"x-apisports-key": APIF_KEY})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print("API-Football request failed:", type(exc).__name__)
        return None
    if data.get("errors"):
        print("API-Football error:", str(data.get("errors"))[:200])
        return None
    resp = data.get("response")
    return resp if isinstance(resp, list) else None


def _fixtures_of_day(day):
    """One call per date, then served from Neon."""
    key = "apif-fixtures:" + day
    cached = neon_get(key)
    if cached is not None:
        return cached
    items = _apif("fixtures", {"date": day})
    if items is None:
        neon_set(key, [], 600)  # failed: do not retry for 10 minutes
        return []
    slim = [
        {
            "id": (i.get("fixture") or {}).get("id"),
            "home": ((i.get("teams") or {}).get("home") or {}).get("name"),
            "away": ((i.get("teams") or {}).get("away") or {}).get("name"),
        }
        for i in items
    ]
    neon_set(key, slim, 6 * 3600)
    return slim


# ------------------------------------------------------------
# PREDICTION -> SLIM (what we store in Neon)
# ------------------------------------------------------------

def _slim(item, fixture_id):
    p = item.get("predictions") or {}
    teams = item.get("teams") or {}

    def side(s):
        t = teams.get(s) or {}
        l5 = t.get("last_5") or {}
        lg = t.get("league") or {}
        fx = lg.get("fixtures") or {}
        gl = lg.get("goals") or {}
        venue = {}
        for v in ("home", "away"):
            venue[v] = {
                "played": (fx.get("played") or {}).get(v),
                "wins": (fx.get("wins") or {}).get(v),
                "draws": (fx.get("draws") or {}).get(v),
                "losses": (fx.get("loses") or {}).get(v),
                "scored": ((gl.get("for") or {}).get("total") or {}).get(v),
                "conceded": ((gl.get("against") or {}).get("total") or {}).get(v),
            }
        return {
            "id": t.get("id"),
            "name": t.get("name"),
            "league_form": lg.get("form"),
            "l5_played": l5.get("played"),
            "l5_for": ((l5.get("goals") or {}).get("for") or {}).get("total"),
            "l5_against": ((l5.get("goals") or {}).get("against") or {}).get("total"),
            "venue": venue,
        }

    h2h = []
    for m in (item.get("h2h") or [])[:10]:
        tm = m.get("teams") or {}
        g = m.get("goals") or {}
        h2h.append(
            {
                "hid": (tm.get("home") or {}).get("id"),
                "aid": (tm.get("away") or {}).get("id"),
                "hg": g.get("home"),
                "ag": g.get("away"),
            }
        )

    return {
        "fixture_id": fixture_id,
        "competition": (item.get("league") or {}).get("name"),
        "percent": p.get("percent") or {},
        "home": side("home"),
        "away": side("away"),
        "h2h": h2h,
    }


def _get_slim(home, away, kickoff, similarity):
    if not APIF_KEY:
        print("FOOTBALL: API_FOOTBALL_KEY is not set on this server")
        return None
    when = kickoff or datetime.now(timezone.utc)
    day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
    key = "apif-pred:%s:%s:%s" % (day, home.lower(), away.lower())

    cached = neon_get(key)
    if cached is not None:
        return cached or None

    best, best_score = None, 0.0
    for f in _fixtures_of_day(day):
        a = similarity(home, f.get("home") or "")
        b = similarity(away, f.get("away") or "")
        score = (a + b) / 2
        if min(a, b) >= 0.7 and score > best_score:
            best, best_score = f, score
    if not best or best_score < 0.80 or not best.get("id"):
        print("FOOTBALL: no API-Football match found for %s vs %s on %s" % (home, away, day))
        neon_set(key, {}, 3600)
        return None

    items = _apif("predictions", {"fixture": best["id"]})
    if not items:
        return None
    slim = _slim(items[0], best["id"])
    neon_set(key, slim, 12 * 3600)
    return slim


# ------------------------------------------------------------
# SLIM -> FACTS (same shape football_data.py already uses)
# ------------------------------------------------------------

def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace("%", ""))
    except Exception:
        return default


def _int(v, default=0):
    n = _num(v)
    return int(n) if n is not None else default


def _recent(s):
    form = "".join(c for c in str(s.get("league_form") or "").upper() if c in "WDL")[-5:]
    n = _int(s.get("l5_played"), 0) or len(form)
    scored = _int(s.get("l5_for"), 0)
    conceded = _int(s.get("l5_against"), 0)
    return {
        "played": len(form),
        "wins": form.count("W"),
        "draws": form.count("D"),
        "losses": form.count("L"),
        "scored": scored,
        "conceded": conceded,
        "avg_scored": scored / n if n else None,
        "avg_conceded": conceded / n if n else None,
    }


def _venue(s, v):
    d = (s.get("venue") or {}).get(v) or {}
    played = _int(d.get("played"), 0)
    scored = _int(d.get("scored"), 0)
    conceded = _int(d.get("conceded"), 0)
    return {
        "played": played,
        "wins": _int(d.get("wins"), 0),
        "draws": _int(d.get("draws"), 0),
        "losses": _int(d.get("losses"), 0),
        "scored": scored,
        "conceded": conceded,
        "avg_scored": scored / played if played else None,
        "avg_conceded": conceded / played if played else None,
    }


def _h2h_stats(items, hid, aid):
    out = {"played": 0, "home_wins": 0, "draws": 0, "away_wins": 0,
           "home_scored": 0, "away_scored": 0}
    for m in items or []:
        if {m.get("hid"), m.get("aid")} != {hid, aid}:
            continue
        if m.get("hg") is None or m.get("ag") is None:
            continue
        hg, ag = _int(m["hg"]), _int(m["ag"])
        out["played"] += 1
        if m["hid"] == hid:
            out["home_scored"] += hg
            out["away_scored"] += ag
            mine, theirs = hg, ag
        else:
            out["home_scored"] += ag
            out["away_scored"] += hg
            mine, theirs = ag, hg
        if mine > theirs:
            out["home_wins"] += 1
        elif mine == theirs:
            out["draws"] += 1
        else:
            out["away_wins"] += 1
    return out


def _pct(v):
    n = _num(v)
    return n / 100.0 if n is not None else None


def _build_facts(slim):
    h, a = slim.get("home") or {}, slim.get("away") or {}
    h_recent, a_recent = _recent(h), _recent(a)
    h_venue, a_venue = _venue(h, "home"), _venue(a, "away")

    hc = [x for x in (h_recent["avg_scored"], a_recent["avg_conceded"]) if x is not None]
    ac = [x for x in (a_recent["avg_scored"], h_recent["avg_conceded"]) if x is not None]
    hl = sum(hc) / len(hc) if hc else None
    al = sum(ac) / len(ac) if ac else None
    if hl is not None:
        hl = max(0.20, min(4.50, hl))
        if h_venue["avg_scored"] is not None:
            hl = hl * 0.65 + h_venue["avg_scored"] * 0.35
    if al is not None:
        al = max(0.20, min(4.00, al))
        if a_venue["avg_scored"] is not None:
            al = al * 0.65 + a_venue["avg_scored"] * 0.35

    pc = slim.get("percent") or {}
    return {
        "home": h.get("name") or "Home",
        "away": a.get("name") or "Away",
        "home_id": h.get("id"),
        "away_id": a.get("id"),
        "form": {"home": "", "away": "", "home_counts": {}, "away_counts": {}},
        "recent": {"home": h_recent, "away": a_recent},
        "venue": {"home": h_venue, "away": a_venue},
        "prediction": {"home": _pct(pc.get("home")), "draw": _pct(pc.get("draw")), "away": _pct(pc.get("away"))},
        "h2h": _h2h_stats(slim.get("h2h"), h.get("id"), a.get("id")),
        "lambda": {"home": hl, "away": al},
        "fixture_id": slim.get("fixture_id"),
        "competition": slim.get("competition"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def get_facts(home, away, kickoff, similarity):
    """Full football facts for one match, or None if not enough data."""
    slim = _get_slim(home, away, kickoff, similarity)
    if not slim:
        return None
    facts = _build_facts(slim)
    played = facts["recent"]["home"]["played"] + facts["recent"]["away"]["played"]
    if played < 4 or not facts["lambda"]["home"] or not facts["lambda"]["away"]:
        print("FOOTBALL: not enough form data for %s vs %s" % (home, away))
        return None
    return facts


def extra_reason(facts, text=""):
    """Win % and H2H sentence added to the pick's reason."""
    if not facts:
        return None
    text = (text or "").lower()
    parts = []
    pr = facts.get("prediction") or {}
    if None not in (pr.get("home"), pr.get("draw"), pr.get("away")) and any(
        w in text for w in ("win", "1up", "2up", "handicap")
    ):
        parts.append(
            "API-Football win chance: %s %.0f%% / draw %.0f%% / %s %.0f%%."
            % (facts["home"], pr["home"] * 100, pr["draw"] * 100, facts["away"], pr["away"] * 100)
        )
    h = facts.get("h2h") or {}
    if h.get("played"):
        parts.append(
            "H2H (last %d): %s %dW, %dD, %s %dW."
            % (h["played"], facts["home"], h["home_wins"], h["draws"], facts["away"], h["away_wins"])
        )
    return " ".join(parts) or None