"""
API-Football evidence + Neon (Postgres) persistent cache for SportyTips.

Render env vars:
  API_FOOTBALL_KEY   key from api-football.com (api-sports.io)
  DATABASE_URL       your existing Neon string
  APIF_DAILY_LIMIT   optional hard stop per day (default 90)
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

# Internal failure records; no debug output is sent to users.
LAST_ERR = ""
FAIL = {}


def _fail(home, away, reason):
    FAIL[f"{home} vs {away}"] = str(reason)
    return None


# ------------------------------------------------------------
# NEON CACHE
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
    if neon_enabled():
        try:
            _run("DELETE FROM football_cache WHERE expires_at < now()")
        except Exception:
            pass


# ------------------------------------------------------------
# SOCCERSAPI REQUESTS
# ------------------------------------------------------------

import difflib
from datetime import timedelta

SOCCERSAPI_KEY = (
    os.getenv("SOCCERSAPI_API_KEY")
    or os.getenv("SOCCERSAPI_KEY")
    or os.getenv("SOCCERSAPI_TOKEN")
    or ""
).strip().strip("\"'")
SOCCERSAPI_BASE = "https://api.soccersapi.com/v2.2"

LAST_ERR = ""
FAIL = {}
LAST_FAIL = ""
LAST_FAIL_KEY = ""


def _fail(home, away, reason):
    global LAST_FAIL, LAST_FAIL_KEY
    LAST_FAIL_KEY = f"{home} vs {away}"
    LAST_FAIL = str(reason)
    FAIL[LAST_FAIL_KEY] = LAST_FAIL
    return None


def _request(resource, params):
    """Small timeout-bound SoccersAPI request; never prints or exposes credentials."""
    global LAST_ERR
    LAST_ERR = ""
    if not SOCCERSAPI_KEY:
        LAST_ERR = "SOCCERSAPI_API_KEY missing on server"
        return None
    q = dict(params or {})
    q.setdefault("lang", "en")
    url = SOCCERSAPI_BASE + "/" + resource.strip("/") + "/?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + SOCCERSAPI_KEY, "Accept": "application/json", "User-Agent": "SportyTips/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        LAST_ERR = "SoccersAPI request failed: " + type(exc).__name__
        return None
    if not isinstance(payload, dict):
        LAST_ERR = "SoccersAPI returned an unexpected response"
        return None
    meta = payload.get("meta") or {}
    if meta.get("msg"):
        LAST_ERR = str(meta.get("msg"))[:180]
    if payload.get("error"):
        LAST_ERR = str(payload.get("error"))[:180]
        return None
    # SoccersAPI can return an empty data array as a valid result.
    if "data" not in payload:
        LAST_ERR = LAST_ERR or "SoccersAPI response had no data field"
        return None
    return payload.get("data")


def _cache_key(*parts):
    return "soccersapi:v1:" + ":".join(str(x or "").strip().lower() for x in parts)


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "events", "fixtures", "items", "last", "overall"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _team_names(item):
    teams = item.get("teams") or {}
    h = teams.get("home") or item.get("home") or {}
    a = teams.get("away") or item.get("away") or {}
    if isinstance(h, str): h = {"name": h}
    if isinstance(a, str): a = {"name": a}
    return h, a


def _score_pair(item):
    scores = item.get("scores") or {}
    # v2.2 fixture responses normally use home_score/away_score.
    hs = scores.get("home_score", scores.get("home"))
    as_ = scores.get("away_score", scores.get("away"))
    if hs in (None, "", "-") or as_ in (None, "", "-"):
        # Some feeds use ft_score as "2-1".
        ft = item.get("ft_score") or scores.get("ft_score")
        if isinstance(ft, str) and "-" in ft:
            bits = ft.split("-", 1)
            hs, as_ = bits[0].strip(), bits[1].strip()
    try:
        return int(float(hs)), int(float(as_))
    except (TypeError, ValueError):
        return None, None


def _team_id(item, side):
    teams = item.get("teams") or {}
    team = teams.get(side) or {}
    return team.get("id") if isinstance(team, dict) else None


def _kickoff_date(kickoff):
    if isinstance(kickoff, datetime):
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        return kickoff.astimezone(timezone.utc).strftime("%Y-%m-%d")
    if isinstance(kickoff, (int, float)):
        return datetime.fromtimestamp(kickoff, timezone.utc).strftime("%Y-%m-%d")
    if isinstance(kickoff, str) and kickoff.strip():
        s = kickoff.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _schedule(day):
    key = _cache_key("schedule", day)
    cached = neon_get(key)
    if isinstance(cached, list):
        return cached
    data = _request("fixtures", {"t": "schedule", "d": day, "utc": 0, "page": 1})
    rows = _as_list(data)
    neon_set(key, rows, 6 * 3600 if rows else 600)
    return rows


def _similarity(a, b, similarity):
    try:
        return float(similarity(a or "", b or ""))
    except Exception:
        return difflib.SequenceMatcher(None, str(a or "").lower(), str(b or "").lower()).ratio()


def _find_match(home, away, kickoff, similarity):
    day = _kickoff_date(kickoff)
    dates = [day]
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dates += [(d + timedelta(days=delta)).strftime("%Y-%m-%d") for delta in (-1, 1)]
    except Exception:
        pass
    best, best_score = None, 0.0
    for date in dates:
        for row in _schedule(date):
            if not isinstance(row, dict):
                continue
            h, a = _team_names(row)
            hn, an = h.get("name", ""), a.get("name", "")
            direct = (_similarity(home, hn, similarity) + _similarity(away, an, similarity)) / 2
            reverse = (_similarity(home, an, similarity) + _similarity(away, hn, similarity)) / 2
            score = max(direct, reverse)
            if score > best_score:
                best, best_score = row, score
    if best and best_score >= 0.76:
        return best
    return None


def _finished_matches(team_data):
    if not isinstance(team_data, dict):
        return []
    rows = team_data.get("last") or []
    if not isinstance(rows, list):
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hs, aw = _score_pair(row)
        if hs is None or aw is None:
            continue
        status = str(row.get("status_name") or "").lower()
        if status and not any(word in status for word in ("finish", "ended", "after penalties")) and str(row.get("status")) not in ("3", "31"):
            continue
        out.append(row)
    return out


def _team_recent(team_id, team_name, venue_side):
    key = _cache_key("lastnext", team_id)
    data = neon_get(key)
    if data is None:
        data = _request("fixtures", {"t": "last_next", "team_id": team_id, "utc": 0})
        if isinstance(data, dict):
            neon_set(key, data, 30 * 60)
    matches = _finished_matches(data)
    matches.sort(key=lambda x: ((x.get("time") or {}).get("timestamp") or 0), reverse=True)
    matches = matches[:5]
    form, scored, conceded = "", 0, 0
    venue_played = venue_wins = venue_draws = venue_losses = venue_scored = venue_conceded = 0
    all_played = all_wins = all_draws = all_losses = all_scored = all_conceded = 0
    for row in matches:
        h, a = _team_names(row)
        hid, aid = _team_id(row, "home"), _team_id(row, "away")
        hs, aw = _score_pair(row)
        if hs is None or aw is None:
            continue
        is_home = str(hid) == str(team_id) or (not hid and h.get("name") == team_name)
        mine, theirs = (hs, aw) if is_home else (aw, hs)
        scored += mine; conceded += theirs
        all_played += 1; all_scored += mine; all_conceded += theirs
        if mine > theirs:
            form += "W"; all_wins += 1
        elif mine == theirs:
            form += "D"; all_draws += 1
        else:
            form += "L"; all_losses += 1
        if (venue_side == "home" and is_home) or (venue_side == "away" and not is_home):
            venue_played += 1; venue_scored += mine; venue_conceded += theirs
            if mine > theirs: venue_wins += 1
            elif mine == theirs: venue_draws += 1
            else: venue_losses += 1
    return {
        "id": team_id, "name": team_name, "league_form": form,
        "l5_played": all_played, "l5_for": all_scored, "l5_against": all_conceded,
        "venue": {venue_side: {"played": venue_played, "wins": venue_wins, "draws": venue_draws,
            "losses": venue_losses, "scored": venue_scored, "conceded": venue_conceded}},
    }


def _h2h_rows(data):
    if isinstance(data, dict):
        # /h2h/?t=teams returns data.home.events.overall in documented v2.2 responses.
        home = data.get("home") or {}
        events = home.get("events") or {}
        rows = events.get("overall") or events.get("home") or []
        if isinstance(rows, list): return rows
        for key in ("overall", "matches", "events"):
            if isinstance(data.get(key), list): return data[key]
    return _as_list(data)


def _get_slim(home, away, kickoff, similarity):
    if not SOCCERSAPI_KEY:
        return _fail(home, away, "SOCCERSAPI_API_KEY missing on server")
    day = _kickoff_date(kickoff)
    cache_key = _cache_key("facts", day, home, away)
    cached = neon_get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached

    match = _find_match(home, away, kickoff, similarity)
    if not match:
        return _fail(home, away, "SoccersAPI could not match these teams to a fixture on/near " + day + (" (" + LAST_ERR + ")" if LAST_ERR else ""))
    teams = match.get("teams") or {}
    ht, at = teams.get("home") or {}, teams.get("away") or {}
    hid, aid = ht.get("id"), at.get("id")
    hname, aname = ht.get("name") or home, at.get("name") or away
    if not hid or not aid:
        # Some fixture-list records omit IDs; request canonical detail by match ID.
        match_id = match.get("id")
        detail = _request("fixtures", {"t": "info", "id": match_id}) if match_id else None
        if isinstance(detail, dict):
            match = detail
            teams = match.get("teams") or {}
            ht, at = teams.get("home") or {}, teams.get("away") or {}
            hid, aid = ht.get("id"), at.get("id")
            hname, aname = ht.get("name") or home, at.get("name") or away
    if not hid or not aid:
        return _fail(home, away, "SoccersAPI fixture response did not include team IDs")

    h_recent = _team_recent(hid, hname, "home")
    a_recent = _team_recent(aid, aname, "away")
    h2h_key = _cache_key("h2h", hid, aid)
    h2h_data = neon_get(h2h_key)
    if h2h_data is None:
        h2h_data = _request("h2h", {"t": "teams", "team1": hid, "team2": aid})
        if h2h_data is not None:
            neon_set(h2h_key, h2h_data, 12 * 3600)
    h2h = []
    for row in _h2h_rows(h2h_data):
        if not isinstance(row, dict): continue
        rh, ra = _team_names(row)
        rs = row.get("scores") or {}
        hs, aw = _score_pair(row)
        if hs is None or aw is None: continue
        h2h.append({"hid": _team_id(row, "home"), "aid": _team_id(row, "away"), "hg": hs, "ag": aw,
                    "home_name": rh.get("name"), "away_name": ra.get("name")})
    slim = {
        "fixture_id": match.get("id"),
        "competition": (match.get("league") or {}).get("name") or match.get("league_name"),
        "percent": {},
        "home": h_recent,
        "away": a_recent,
        "h2h": h2h,
    }
    neon_set(cache_key, slim, 45 * 60)
    return slim


# ------------------------------------------------------------
# SOCCERSAPI DATA -> EXISTING FACTS SCHEMA
# ------------------------------------------------------------

def _num(v, default=None):
    try:
        if v is None or v == "": return default
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
    return {"played": _int(s.get("l5_played"), len(form)), "wins": form.count("W"), "draws": form.count("D"),
            "losses": form.count("L"), "scored": scored, "conceded": conceded,
            "avg_scored": scored / n if n else None, "avg_conceded": conceded / n if n else None}


def _venue(s, v):
    d = (s.get("venue") or {}).get(v) or {}
    played = _int(d.get("played"), 0); scored = _int(d.get("scored"), 0); conceded = _int(d.get("conceded"), 0)
    return {"played": played, "wins": _int(d.get("wins"), 0), "draws": _int(d.get("draws"), 0),
            "losses": _int(d.get("losses"), 0), "scored": scored, "conceded": conceded,
            "avg_scored": scored / played if played else None, "avg_conceded": conceded / played if played else None}


def _h2h_stats(items, hid, aid):
    out = {"played": 0, "home_wins": 0, "draws": 0, "away_wins": 0, "home_scored": 0, "away_scored": 0}
    for m in items or []:
        # SoccersAPI H2H events can use team IDs or omit them; map by names when IDs are absent.
        mhid, maid = m.get("hid"), m.get("aid")
        if mhid is not None and maid is not None and {str(mhid), str(maid)} != {str(hid), str(aid)}:
            continue
        if m.get("hg") is None or m.get("ag") is None: continue
        hg, ag = _int(m["hg"]), _int(m["ag"])
        out["played"] += 1
        if mhid is not None and str(mhid) == str(hid): mine, theirs = hg, ag
        elif mhid is not None and str(mhid) == str(aid): mine, theirs = ag, hg
        else:
            # H2H endpoint is requested team1=home, team2=away; use listed order.
            mine, theirs = hg, ag
        out["home_scored"] += mine; out["away_scored"] += theirs
        if mine > theirs: out["home_wins"] += 1
        elif mine == theirs: out["draws"] += 1
        else: out["away_wins"] += 1
    return out


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
        if h_venue["avg_scored"] is not None: hl = hl * 0.65 + h_venue["avg_scored"] * 0.35
    if al is not None:
        al = max(0.20, min(4.00, al))
        if a_venue["avg_scored"] is not None: al = al * 0.65 + a_venue["avg_scored"] * 0.35
    return {"home": h.get("name") or "Home", "away": a.get("name") or "Away", "home_id": h.get("id"), "away_id": a.get("id"),
            "form": {"home": "", "away": "", "home_counts": {}, "away_counts": {}},
            "recent": {"home": h_recent, "away": a_recent}, "venue": {"home": h_venue, "away": a_venue},
            "prediction": {"home": None, "draw": None, "away": None},
            "h2h": _h2h_stats(slim.get("h2h"), h.get("id"), a.get("id")),
            "lambda": {"home": hl, "away": al}, "fixture_id": slim.get("fixture_id"),
            "competition": slim.get("competition"), "retrieved_at": datetime.now(timezone.utc).isoformat()}


def get_facts(home, away, kickoff, similarity):
    global LAST_FAIL, LAST_FAIL_KEY
    FAIL.pop(f"{home} vs {away}", None)
    LAST_FAIL = ""; LAST_FAIL_KEY = ""
    slim = _get_slim(home, away, kickoff, similarity)
    if not slim: return None
    facts = _build_facts(slim)
    played = facts["recent"]["home"]["played"] + facts["recent"]["away"]["played"]
    if played < 4 or not facts["lambda"]["home"] or not facts["lambda"]["away"]:
        return _fail(home, away, "not enough recent results from SoccersAPI (played=%d)" % played)
    return facts


def extra_reason(facts, text=""):
    if not facts: return None
    h2h = facts.get("h2h") or {}
    parts = []
    if h2h.get("played"):
        parts.append("SoccersAPI H2H (%d meetings): %s %d wins, %d draws; %s %d wins." % (
            h2h["played"], facts["home"], h2h["home_wins"], h2h["draws"], facts["away"], h2h["away_wins"]))
    for side, label in (("home", facts["home"]), ("away", facts["away"])):
        r = (facts.get("recent") or {}).get(side) or {}
        if r.get("played"):
            parts.append("%s recent form: %d wins, %d draws, %d losses in %d matches; %d scored and %d conceded." % (
                label, r.get("wins", 0), r.get("draws", 0), r.get("losses", 0), r.get("played", 0),
                r.get("scored", 0), r.get("conceded", 0)))
    return " ".join(parts) or None
