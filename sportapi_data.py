"""
sportapi_data.py - switches SportyTips from football-data.org to sportapi.ai

Add ONE line to app.py, after football_data is imported:

    import sportapi_data   # football evidence now comes from sportapi.ai

What it does
    1. Fetches fixtures from sportapi.ai (one call per day, not per team).
    2. Builds each team's last 5 results and form from a saved history of
       past days, so team form costs ZERO extra calls.
    3. Caches everything in memory, then in a Google Sheet, then (only if
       both miss) calls the API.
    4. Stops calling the API before the daily/monthly limit is reached and
       serves the last saved data instead.

Environment variables (Render):
    SPORTAPI_KEY          your sportapi.ai key (starts with sk_live_)
    SHEET_CACHE_URL       Apps Script web app URL (see sheet_cache.gs)
    SHEET_CACHE_SECRET    the SECRET you set inside sheet_cache.gs
Optional:
    SPORTAPI_RESERVE_DAY    calls kept unused each day        (default 100)
    SPORTAPI_RESERVE_MONTH  calls kept unused each month      (default 300)
    SPORTAPI_HISTORY_DAYS   days of past results to keep      (default 45)
    SPORTAPI_FETCH_PER_RUN  history days fetched per request  (default 4)
    SPORTAPI_H2H            1 = use head-to-head, 0 = skip it (default 1)

Command line (no API calls unless you run backfill):
    python sportapi_data.py test-sheet     check the Google Sheet works
    python sportapi_data.py status         show saved history and quota
    python sportapi_data.py backfill 45    one-time fill of past days
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from datetime import datetime, timedelta, timezone

import football_data as fd


# ============================================================
# SETTINGS
# ============================================================

API_BASE = "https://sportapi.ai/api"
API_KEY = os.getenv("SPORTAPI_KEY", "").strip()

SHEET_URL = os.getenv("SHEET_CACHE_URL", "").strip()
SHEET_SECRET = os.getenv("SHEET_CACHE_SECRET", "").strip()

RESERVE_DAY = int(os.getenv("SPORTAPI_RESERVE_DAY", "100"))
RESERVE_MONTH = int(os.getenv("SPORTAPI_RESERVE_MONTH", "300"))
HISTORY_DAYS = int(os.getenv("SPORTAPI_HISTORY_DAYS", "45"))
FETCH_PER_RUN = int(os.getenv("SPORTAPI_FETCH_PER_RUN", "4"))
USE_H2H = os.getenv("SPORTAPI_H2H", "1") != "0"

CHUNK = 40000          # a Google Sheet cell holds at most 50,000 characters
RESULTS_PER_TEAM = 5   # same as before, so predictions do not change
H2H_TTL = 14 * 86400
HISTORY_KEY = "history:v1"

FINISHED = {"FT", "AET", "PENS"}
TERMINAL = FINISHED | {"Postponed", "Cancelled"}


# ============================================================
# TIME HELPERS
# ============================================================

def _today_local():
    """sportapi.ai keeps its calendar in Central European time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Berlin")).date()
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=1)).date()


def _day_ttl(date_string):
    """How long a day's fixture list can be trusted."""
    try:
        day = datetime.strptime(date_string, "%Y-%m-%d").date()
    except Exception:
        return 1800
    today = _today_local()
    if day == today:
        return 30 * 60
    if day > today:
        return 3 * 3600
    if day == today - timedelta(days=1):
        return 3 * 3600
    return 30 * 86400


# ============================================================
# GOOGLE SHEET CACHE
# ============================================================

_MEM = {}
_sheet_fail = 0
_sheet_off_until = 0.0


def _sheet_enabled():
    return bool(SHEET_URL and SHEET_SECRET) and time.time() >= _sheet_off_until


def _sheet_call(payload, timeout=25):
    """One request to the Apps Script web app. Returns a dict or None."""
    global _sheet_fail, _sheet_off_until

    if not _sheet_enabled():
        return None

    body = dict(payload)
    body["secret"] = SHEET_SECRET

    try:
        request = urllib.request.Request(
            SHEET_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, dict) or not data.get("ok"):
            raise ValueError("sheet refused")

        _sheet_fail = 0
        return data

    except Exception as exc:
        _sheet_fail += 1
        print(f"Sheet cache problem: {type(exc).__name__}")
        # After 3 failures in a row, leave the Sheet alone for 5 minutes so
        # ticket building never hangs waiting for it.
        if _sheet_fail >= 3:
            _sheet_off_until = time.time() + 300
            _sheet_fail = 0
        return None


def sheet_get(key):
    """Returns (value, expired) or (None, False) when nothing is saved."""
    data = _sheet_call({"action": "get", "key": key})
    if not data or not data.get("found"):
        return None, False
    try:
        value = json.loads("".join(data.get("parts") or []))
    except Exception:
        return None, False
    return value, bool(data.get("expired"))


def sheet_set(key, value, ttl):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    data = _sheet_call(
        {
            "action": "set",
            "key": key,
            "parts": parts,
            "expires": int(time.time() + ttl),
        },
        timeout=60,
    )
    return bool(data)


def cached(key, ttl, fetch):
    """
    memory -> Google Sheet -> API.
    If the API is blocked or fails, the last saved (even expired) value is
    returned so the bot keeps working.
    """
    now = time.time()
    hit = _MEM.get(key)
    if hit and hit[0] > now:
        return hit[1]

    mem_ttl = min(ttl, 1800)

    value, expired = sheet_get(key)
    if value is not None and not expired:
        _MEM[key] = (now + mem_ttl, value)
        return value

    fresh = fetch()
    if fresh is not None:
        _MEM[key] = (now + mem_ttl, fresh)
        sheet_set(key, fresh, ttl)
        return fresh

    if value is not None:  # stale copy beats nothing
        _MEM[key] = (now + 300, value)
    return value


# ============================================================
# API CALLS + QUOTA GUARD
# ============================================================

_quota = None            # loaded lazily, kept in the Sheet too
_blocked_until = 0.0


def _load_quota():
    global _quota
    if _quota is None:
        saved, _ = sheet_get("meta:quota")
        _quota = saved if isinstance(saved, dict) else {}
    return _quota


def _note_quota(headers):
    """Read the rate-limit headers from every response."""
    global _quota
    q = _load_quota()

    def num(name):
        try:
            return int(headers.get(name))
        except Exception:
            return None

    day = num("X-RateLimit-Remaining")
    reset = num("X-RateLimit-Reset")
    month = num("X-RateLimit-Month-Remaining")
    month_reset = num("X-RateLimit-Month-Reset")

    if day is not None:
        q["day"], q["reset"] = day, reset
    if month is not None:
        q["month"], q["month_reset"] = month, month_reset

    q["seen"] = int(time.time())
    _quota = q
    sheet_set("meta:quota", q, 2 * 86400)


def quota_ok(extra_reserve=0):
    """False when calling the API now would eat into the safety reserve."""
    now = time.time()
    if now < _blocked_until:
        return False

    q = _load_quota()

    day, reset = q.get("day"), q.get("reset")
    if day is not None and reset and now < reset:
        if day <= RESERVE_DAY + extra_reserve:
            return False

    month, month_reset = q.get("month"), q.get("month_reset")
    if month is not None and month_reset and now < month_reset:
        if month <= RESERVE_MONTH + extra_reserve:
            return False

    return True


def _api_get(path, extra_reserve=0):
    """One sportapi.ai request. Returns the JSON body or None."""
    global _blocked_until

    if not API_KEY or not quota_ok(extra_reserve):
        return None

    request = urllib.request.Request(
        API_BASE + path,
        headers={
            "X-Api-Key": API_KEY,
            "Accept": "application/json",
            "User-Agent": "SportyTips/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            _note_quota(response.headers)
            body = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        _note_quota(exc.headers)
        if exc.code == 429:
            try:
                wait = int(exc.headers.get("Retry-After") or 3600)
            except Exception:
                wait = 3600
            _blocked_until = time.time() + wait
            print("sportapi.ai limit reached - using saved data only")
        else:
            print(f"sportapi.ai error {exc.code} on {path}")
        return None

    except Exception as exc:
        print(f"sportapi.ai request failed: {type(exc).__name__}")
        return None

    if not isinstance(body, dict) or not body.get("success"):
        return None
    return body


# ============================================================
# SHAPE CONVERSION
# sportapi.ai fixture  ->  the football-data.org shape that the rest of
# football_data.py already understands (homeTeam, score.fullTime, ...)
# ============================================================

_FX_TEAMS = {}   # fixture id -> (home id, away id), used for head-to-head


def _compact(r):
    return {
        "i": r.get("id"),
        "h": r.get("home_id"),
        "a": r.get("away_id"),
        "hn": r.get("home_team") or "",
        "an": r.get("away_team") or "",
        "hs": r.get("home_score"),
        "as": r.get("away_score"),
        "k": r.get("kickoff_utc")
        or (r.get("date") + "T12:00:00Z" if r.get("date") else None),
        "l": r.get("league_id"),
        "ln": r.get("league_name") or "",
        "s": r.get("status") or "",
    }


def _status(s):
    if s in FINISHED:
        return "FINISHED"
    return {
        "LIVE": "IN_PLAY",
        "Scheduled": "TIMED",
        "Postponed": "POSTPONED",
        "Cancelled": "CANCELLED",
        "Suspended": "SUSPENDED",
    }.get(s, "SCHEDULED")


def _to_fd(c):
    if c.get("i") is not None:
        _FX_TEAMS[c["i"]] = (c.get("h"), c.get("a"))
    return {
        "id": c.get("i"),
        "utcDate": c.get("k"),
        "status": _status(c.get("s")),
        "homeTeam": {"id": c.get("h"), "name": c.get("hn")},
        "awayTeam": {"id": c.get("a"), "name": c.get("an")},
        "score": {"fullTime": {"home": c.get("hs"), "away": c.get("as")}},
        "competition": {"id": c.get("l"), "name": c.get("ln")},
    }


def _is_result(c):
    return (
        c.get("s") in FINISHED
        and c.get("hs") is not None
        and c.get("as") is not None
        and c.get("h") is not None
        and c.get("a") is not None
    )


# ============================================================
# FIXTURES FOR ONE DAY  (replaces football_data._fd4_fixtures_for)
# ============================================================

def _fetch_day(date_string):
    body = _api_get(f"/fixtures/date/{date_string}")
    if body is None:
        return None
    return [_compact(r) for r in (body.get("data") or []) if isinstance(r, dict)]


def sp_fixtures_for(date_string):
    key = f"fx:{date_string}"
    compact = cached(key, _day_ttl(date_string), lambda: _fetch_day(date_string))
    return [_to_fd(c) for c in (compact or [])]


# ============================================================
# PAST RESULTS HISTORY  (this is what saves most of your calls)
# ============================================================

_hist = None
_last_try = {}
_last_ensure = 0.0


def _load_history():
    global _hist
    if _hist is None:
        saved, _ = sheet_get(HISTORY_KEY)
        _hist = saved if isinstance(saved, dict) and "days" in saved else {"days": {}}
    return _hist


def _save_history():
    sheet_set(HISTORY_KEY, _load_history(), 90 * 86400)


def ensure_history(days=None, max_fetch=None):
    """
    Make sure the last `days` days of finished matches are saved.
    Each missing day costs one API call, only once, then it is kept.
    max_fetch limits how many days are fetched per run (None = no limit).
    """
    days = days or HISTORY_DAYS
    hist = _load_history()
    today = _today_local()

    fetched = 0
    changed = False

    for n in range(1, days + 1):
        day = (today - timedelta(days=n)).isoformat()
        if day in hist["days"]:
            continue
        if max_fetch is not None and fetched >= max_fetch:
            break
        if _last_try.get(day, 0) > time.time() - 3 * 3600:
            continue
        if not quota_ok():
            break

        _last_try[day] = time.time()
        items = _fetch_day(day)
        fetched += 1
        if items is None:
            continue

        # Yesterday may still have unfinished games, so only lock a day in
        # once it is 2+ days old or every match on it is settled.
        settled = n >= 2 or all(c["s"] in TERMINAL for c in items)
        if settled:
            hist["days"][day] = [c for c in items if _is_result(c)]
            changed = True

    cutoff = (today - timedelta(days=days + 3)).isoformat()
    for day in [d for d in hist["days"] if d < cutoff]:
        del hist["days"][day]
        changed = True

    if changed:
        _save_history()
        _index_cache.clear()

    return fetched


_index_cache = {}


def _team_index():
    hit = _index_cache.get("idx")
    if hit and hit[0] > time.time():
        return hit[1]

    pool = {}
    for items in _load_history()["days"].values():
        for c in items:
            pool[c["i"]] = c

    # Results from today/yesterday that are only in the day caches.
    for key, (expires, value) in list(_MEM.items()):
        if key.startswith("fx:") and isinstance(value, list):
            for c in value:
                if _is_result(c):
                    pool[c["i"]] = c

    by_team = {}
    for c in pool.values():
        by_team.setdefault(c["h"], []).append(c)
        by_team.setdefault(c["a"], []).append(c)
    for lst in by_team.values():
        lst.sort(key=lambda c: c.get("k") or "", reverse=True)

    _index_cache["idx"] = (time.time() + 120, by_team)
    return by_team


def sp_team_results(team_id):
    """Last results for a team, from saved history (no API call)."""
    global _last_ensure
    if not team_id:
        return []

    if time.time() - _last_ensure > 600:
        _last_ensure = time.time()
        try:
            ensure_history(max_fetch=FETCH_PER_RUN)
        except Exception as exc:
            print(f"History refresh failed: {type(exc).__name__}")

    rows = _team_index().get(team_id) or []
    return [_to_fd(c) for c in rows[:RESULTS_PER_TEAM]]


# ============================================================
# HEAD TO HEAD  (replaces football_data._fd4_h2h)
# ============================================================

def sp_h2h(match_id):
    if not USE_H2H or not match_id:
        return []

    teams = _FX_TEAMS.get(match_id)
    if not teams or not all(teams):
        return []

    first, second = sorted(teams)
    key = f"h2h:{first}-{second}"

    def fetch():
        # Head-to-head is a bonus, so keep a bigger reserve for it.
        body = _api_get(f"/fixtures/h2h/{first}/{second}", extra_reserve=300)
        if body is None:
            return None
        return [_compact(r) for r in (body.get("data") or []) if isinstance(r, dict)]

    compact = cached(key, H2H_TTL, fetch)
    items = [c for c in (compact or []) if _is_result(c)]
    items.sort(key=lambda c: c.get("k") or "", reverse=True)
    return [_to_fd(c) for c in items[:10]]


# ============================================================
# SWITCH football_data.py OVER TO sportapi.ai
# ============================================================

def install():
    fd._fd4_fixtures_for = sp_fixtures_for
    fd._fd4_team_results = sp_team_results
    fd._fd4_h2h = sp_h2h

    # New cache version, so old football-data.org entries in Redis/SQLite
    # are never mixed with the new data.
    fd.CACHE_VERSION = os.getenv("FOOTBALL_CACHE_VERSION", "sp1")

    original_diag = fd.diag_summary

    def diag_summary():
        info = dict(original_diag())
        info["data_source"] = "sportapi.ai"
        info["sheet_cache"] = bool(SHEET_URL and SHEET_SECRET)
        info["history_days_saved"] = len(_load_history()["days"])
        info["quota"] = dict(_load_quota())
        return info

    fd.diag_summary = diag_summary

    if not API_KEY:
        print("WARNING: SPORTAPI_KEY is not set - no fresh football data")
    if not (SHEET_URL and SHEET_SECRET):
        print("WARNING: Google Sheet cache is not set - cache is memory only")


install()


# ============================================================
# COMMAND LINE
# ============================================================

def _cli(argv):
    command = argv[1] if len(argv) > 1 else ""

    if command == "test-sheet":
        ok = sheet_set("test:hello", {"hello": "world"}, 600)
        value, expired = sheet_get("test:hello")
        print("write:", ok, "| read:", value, "| expired:", expired)
        print("Sheet works." if value == {"hello": "world"} else "Sheet NOT working.")

    elif command == "status":
        hist = _load_history()["days"]
        print("Days saved:", len(hist), "| matches saved:", sum(len(v) for v in hist.values()))
        print("Quota:", _load_quota())

    elif command == "backfill":
        days = int(argv[2]) if len(argv) > 2 else HISTORY_DAYS
        print(f"Filling up to {days} past days (one API call per missing day)...")
        used = ensure_history(days=days, max_fetch=None)
        print("API calls used:", used, "| quota now:", _load_quota())

    else:
        print(__doc__)


if __name__ == "__main__":
    _cli(sys.argv)
