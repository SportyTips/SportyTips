"""SamuelBet AI - Telegram football bot (v4: bulk odds, AI chat, safe mode)."""

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = (os.getenv("FOOTBALL_DATA_API_KEY") or "").strip().strip("\"'")
# Backward-compatible alias; no API-Football service is used.
API_FOOTBALL_KEY = FOOTBALL_DATA_API_KEY
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

AI_MODEL = "claude-haiku-4-5-20251001"  # fast and cheap
MAX_HISTORY = 12  # messages remembered per chat (keep even)

TELEGRAM_TIMEOUT = 40
API_TIMEOUT = 15
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
API_BASE_URL = FOOTBALL_DATA_BASE_URL

MAX_MESSAGE_LENGTH = 3500
MAX_FIXTURES_TO_SHOW = 100

# Scanning rules
MIN_HOURS_BEFORE_KICKOFF = 1  # ignore matches starting in less than 1 hour
MAX_DAYS_AHEAD = 2  # free API plan only sees today + tomorrow
CACHE_SECONDS = 600  # reuse fixture data for 10 minutes

# Nigeria time (WAT = UTC+1)
LOCAL_TZ = timezone(timedelta(hours=1))
LOCAL_TZ_NAME = "WAT"
API_TIMEZONE = "Africa/Lagos"

# API usage
USE_PREDICTIONS = False  # True = also call /predictions (uses more requests)
MAX_PREDICTIONS = 4  # max prediction calls per ticket when enabled
MAX_ODDS_PAGES = 4  # max bulk-odds pages per date (10 matches per page)
MAX_SINGLE_FALLBACK = 3  # single-match odds calls for top matches the bulk missed

# Which kinds of picks the bot may use.
# win = team to win (1X2)
# goals = Over 1.5 goals
# btts = both teams to score (Yes or No)
# dc = double chance (added automatically in safe mode)
# either_half = team to win either half (many bookies do not list it)
# corners = over X corners (many bookies do not list it)
# Keep corners / either_half OFF unless your bookie really offers them.
ENABLED_MARKETS = {"win", "goals", "btts"}

# Risk modes: say "safe", "safer" or "risky" in your message.
RISK_PROFILES = {
    "safe": {
        "min_prob": 0.65,
        "min_odds": 1.20,
        "overshoot": 1.15,
        "markets": {"win", "goals", "btts", "dc"},
        "extra_matches": 4,
    },
    "normal": {
        "min_prob": 0.55,
        "min_odds": 1.20,
        "overshoot": 1.35,
        "markets": None,  # use ENABLED_MARKETS
        "extra_matches": 0,
    },
    "risky": {
        "min_prob": 0.45,
        "min_odds": 1.60,  # fewer legs, bigger odds per leg
        "overshoot": 1.60,
        "markets": None,
        "extra_matches": 0,
    },
}


# SportyBet check.
# OFF = the bot cannot see SportyBet, so it only uses common markets
# (winner, over 1.5, both teams to score, double chance) and warns you.
# ON = every pick is checked on SportyBet. A pick (or a whole match) that
# SportyBet does not offer is DROPPED. If the check fails, it is dropped too.
# Turn it on after you fill in sportybet_provider.py: USE_SPORTYBET=1
USE_SPORTYBET = os.getenv("USE_SPORTYBET", "0") == "1"
SPORTYBET_PROVIDER = None
if USE_SPORTYBET:
    from sportybet_provider import SportyBetProvider
    SPORTYBET_PROVIDER = SportyBetProvider()


# ============================================================
# ALLOWED LEAGUES
# ============================================================

ALLOWED_LEAGUES = {
    39: "Premier League",
    40: "Championship",
    140: "La Liga",
    78: "Bundesliga",
    135: "Serie A",
    61: "Ligue 1",
    94: "Liga Portugal",
    203: "SÃ¼per Lig",
    2: "UEFA Champions League",
    3: "UEFA Europa League",
    848: "UEFA Conference League",
}

# Popular leagues that get analysed before small leagues
WELL_KNOWN_LEAGUE_IDS = {
    71, 72, 128, 239, 41, 42, 88, 144, 179, 253, 262, 307, 98,
}

# True = scan every league in the world (women's games are still skipped).
# False = only ALLOWED_LEAGUES + the international competitions below.
ALL_LEAGUES = True

# Extra competitions matched by NAME (no IDs needed).
# Only international competitions are matched (country World/Europe/Africa...).
EXTRA_COMPETITION_NAMES = [
    "nations league",
    "africa cup of nations",
    "u21",
    "u-21",
]
EXCLUDED_NAME_WORDS = ["women"]
INTERNATIONAL_COUNTRIES = {
    "world", "europe", "africa", "asia",
    "south-america", "north-america", "oceania",
}

# Words the user can type to pick extra competitions
EXTRA_KEYWORDS = [
    (("nations league",), "nations league"),
    (("afcon", "africa cup of nations", "africa cup"), "africa cup of nations"),
    (("u21", "u-21", "under 21", "under-21"), "u21"),
    (("brazil", "brazilian"), "brazil"),
    (("argentina", "argentinian"), "argentina"),
    (("colombia", "colombian"), "colombia"),
    (("england", "english"), "england"),
    (("scotland", "scottish"), "scotland"),
    (("netherlands", "dutch", "eredivisie"), "netherlands"),
    (("belgium", "belgian"), "belgium"),
    (("usa", "mls"), "usa"),
    (("mexico", "mexican"), "mexico"),
    (("nigeria", "nigerian"), "nigeria"),
    (("saudi", "saudi arabia"), "saudi"),
    (("japan", "japanese"), "japan"),
    (("chile", "chilean"), "chile"),
    (("ecuador",), "ecuador"),
    (("peru",), "peru"),
    (("uruguay",), "uruguay"),
]

# Words the user can type to pick a league
LEAGUE_KEYWORDS = [
    (("premier league", "premier", "epl"), 39),
    (("championship",), 40),
    (("la liga", "laliga", "spain", "spanish"), 140),
    (("bundesliga", "german", "germany"), 78),
    (("serie a", "italy", "italian"), 135),
    (("ligue 1", "france", "french"), 61),
    (("liga portugal", "portugal", "portuguese"), 94),
    (("super lig", "sÃ¼per lig", "turkey", "turkish"), 203),
    (("champions league", "champions"), 2),
    (("europa league", "europa"), 3),
    (("conference league", "conference"), 848),
]


class BotError(Exception):
    pass


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, params=None):
    if not BOT_TOKEN:
        raise BotError("TELEGRAM_BOT_TOKEN is missing.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urlencode(params).encode("utf-8") if params else None

    request = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urlopen(request, timeout=TELEGRAM_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BotError(f"Telegram HTTP error {exc.code}: {body}")
    except URLError as exc:
        raise BotError(f"Telegram connection error: {exc}")

    if not result.get("ok"):
        raise BotError(f"Telegram API error: {result}")

    return result


def split_text(text, limit=MAX_MESSAGE_LENGTH):
    """Split long text into chunks without cutting lines."""
    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"

    if current.strip():
        chunks.append(current.rstrip())

    return chunks


def send_message(chat_id, text):
    """Send HTML text. Long text is split automatically."""
    for chunk in split_text(text):
        telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )


# ============================================================
# API-FOOTBALL
# ============================================================

_call_times = []
_api_remaining = None  # daily requests left (from API headers)
MAX_CALLS_PER_MINUTE = 9  # free plan allows 10 per minute


def _throttle():
    """Wait if we are close to the per-minute request limit."""
    now = time.time()
    while _call_times and now - _call_times[0] > 60:
        _call_times.pop(0)

    if len(_call_times) >= MAX_CALLS_PER_MINUTE:
        wait = 60 - (now - _call_times[0]) + 0.5
        if wait > 0:
            time.sleep(wait)

    _call_times.append(time.time())


def football_request(endpoint, params=None, retries=3):
    global _api_remaining

    if not API_FOOTBALL_KEY:
        raise BotError("API_FOOTBALL_KEY is missing.")

    url = f"{API_BASE_URL}/{endpoint}"
    if params:
        url += "?" + urlencode(params)

    for attempt in range(retries + 1):
        _throttle()

        request = Request(
            url,
            method="GET",
            headers={"x-apisports-key": API_FOOTBALL_KEY},
        )

        try:
            with urlopen(request, timeout=API_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("x-ratelimit-requests-remaining")
                if remaining is not None and str(remaining).isdigit():
                    _api_remaining = int(remaining)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < retries:
                time.sleep(20)
                continue
            raise BotError(f"API-Football HTTP error {exc.code}: {body}")
        except URLError as exc:
            raise BotError(f"API-Football connection error: {exc}")

        errors = result.get("errors")
        if errors:
            text = str(errors).lower()
            if "request limit" in text:
                raise BotError("Daily API limit reached. It resets at 1:00 AM WAT.")
            if ("rate" in text or "too many" in text) and attempt < retries:
                time.sleep(20)
                continue
            raise BotError(f"API-Football error: {errors}")

        return result

    raise BotError("API-Football request failed.")


def get_fixtures_for_date(date_string):
    """All fixtures for one date, using Nigeria time for the date."""
    result = football_request(
        "fixtures",
        {"date": date_string, "timezone": API_TIMEZONE},
    )
    return result.get("response", [])


def is_extra_competition(league):
    """International competitions matched by name (Nations League etc.)."""
    name = str(league.get("name", "")).lower()
    country = str(league.get("country", "")).lower()

    if any(word in name for word in EXCLUDED_NAME_WORDS):
        return False
    if country not in INTERNATIONAL_COUNTRIES:
        return False

    return any(key in name for key in EXTRA_COMPETITION_NAMES)


def is_allowed_league(league):
    name = str(league.get("name", "")).lower()

    if any(word in name for word in EXCLUDED_NAME_WORDS):
        return False
    if ALL_LEAGUES:
        return True
    if league.get("id") in ALLOWED_LEAGUES:
        return True

    return is_extra_competition(league)


def filter_allowed_fixtures(fixtures):
    return [
        f for f in fixtures
        if is_allowed_league(f.get("league", {}))
    ]


# ============================================================
# API PLAN LIMIT (free plans only see a few dates)
# ============================================================

_plan_range = None  # (first_date, last_date) as ISO strings
_scan_notes = []


def note_plan_limit(error_text):
    """If the error is a plan date limit, remember the allowed range."""
    global _plan_range

    match = re.search(
        r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", error_text
    )
    if "plan" in error_text.lower() and match:
        _plan_range = (match.group(1), match.group(2))
        return True
    return False


def plan_note():
    if _plan_range:
        return (
            "â¹ï¸ Your API-Football plan only allows dates from "
            f"{_plan_range[0]} to {_plan_range[1]}, "
            "so later days were skipped."
        )
    return ""


# ============================================================
# CACHE (saves your daily API calls)
# ============================================================

_fixture_cache = {}


def get_allowed_fixtures_cached(date_string):
    cached = _fixture_cache.get(date_string)

    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    fixtures = filter_allowed_fixtures(get_fixtures_for_date(date_string))
    _fixture_cache[date_string] = (time.time(), fixtures)

    # Keep the cache small
    if len(_fixture_cache) > 20:
        oldest = min(_fixture_cache, key=lambda k: _fixture_cache[k][0])
        _fixture_cache.pop(oldest, None)

    return fixtures


# ============================================================
# FIXTURE HELPERS
# ============================================================

def parse_fixture_time(fixture):
    fixture_data = fixture.get("fixture", {})
    timestamp = fixture_data.get("timestamp")

    if timestamp:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    date_string = fixture_data.get("date")
    if not date_string:
        return None

    try:
        return datetime.fromisoformat(date_string.replace("Z", "+00:00"))
    except ValueError:
        return None


def league_name_of(fixture):
    league = fixture.get("league", {})
    league_id = league.get("id")

    if league_id in ALLOWED_LEAGUES:
        return ALLOWED_LEAGUES[league_id]

    name = league.get("name", "Unknown")
    country = league.get("country", "")

    if country and country.lower() not in INTERNATIONAL_COUNTRIES:
        return f"{name} ({country})"
    return name


def league_priority(fixture):
    """Your main leagues first, then international, then everything else."""
    league = fixture.get("league", {})

    if league.get("id") in ALLOWED_LEAGUES:
        return 0
    if is_extra_competition(league) or league.get("id") in WELL_KNOWN_LEAGUE_IDS:
        return 1
    return 2


# ============================================================
# UNDERSTAND THE USER'S REQUEST
# ============================================================

SAFE_WORDS = r"\b(safe|safer|safest|secure|low[- ]risk|bankers?)\b"
RISKY_WORDS = r"\b(risky|riskier|riskiest|high[- ]risk|bold|aggressive|long ?shots?)\b"


def parse_request(text):
    """
    Reads things like:
      "give me 15 odds for today only"
      "best 5 days games for 30 odds"
      "5 picks tomorrow premier league"
      "safe 10 odds tomorrow"

    Returns a dict with: start, end, label, target_odds, picks, league_ids,
    name_filters, risk
    """
    message = text.lower()

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    start = now + timedelta(hours=MIN_HOURS_BEFORE_KICKOFF)
    end = now + timedelta(days=1)
    label = "next 24 hours"

    # ---------- time window ----------
    days_match = re.search(r"(\d+)\s*days?\b", message)

    if days_match:
        days = max(1, min(int(days_match.group(1)), MAX_DAYS_AHEAD))
        end = now + timedelta(days=days)
        label = f"next {days} day" + ("s" if days != 1 else "")

    elif re.search(r"\bweekend\b", message):
        weekday = local_now.weekday()  # Monday=0 ... Sunday=6
        if weekday == 6:
            window_start = local_midnight
            window_days = 1
        else:
            window_start = local_midnight + timedelta(days=(5 - weekday) % 7)
            window_days = 2
        start = max(start, window_start)
        end = window_start + timedelta(days=window_days)
        label = "this weekend"

    elif re.search(r"\btomorrow\b", message):
        window_start = local_midnight + timedelta(days=1)
        start = max(start, window_start)
        end = window_start + timedelta(days=1)
        label = "tomorrow"

    elif re.search(r"\btonight\b", message):
        window_start = local_midnight + timedelta(hours=17)
        start = max(start, window_start)
        end = local_midnight + timedelta(days=1, hours=3)
        label = "tonight"

    elif re.search(r"\btoday\b", message):
        end = local_midnight + timedelta(days=1)
        label = "today"

    elif re.search(r"\bweek\b", message):
        end = now + timedelta(days=MAX_DAYS_AHEAD)
        label = f"next {MAX_DAYS_AHEAD} days"

    # ---------- target odds ----------
    target_odds = None
    odds_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:total\s+)?odds?\b", message)
    if not odds_match:
        odds_match = re.search(r"\bodds?\s*(?:of|:|=)?\s*(\d+(?:\.\d+)?)", message)
    if odds_match:
        value = float(odds_match.group(1))
        if 1.01 <= value <= 100000:
            target_odds = value

    # ---------- number of picks ----------
    picks = None
    picks_match = re.search(
        r"(\d+)\s*(?:picks?|games?|matches|match|selections?|predictions?|tips?|legs?|teams?)\b",
        message,
    )
    if picks_match:
        value = int(picks_match.group(1))
        if 1 <= value <= 30:
            picks = value

    # ---------- risk level ----------
    risk = "normal"
    if re.search(SAFE_WORDS, message):
        risk = "safe"
    elif re.search(RISKY_WORDS, message):
        risk = "risky"

    # ---------- leagues ----------
    league_ids = []
    for keywords, league_id in LEAGUE_KEYWORDS:
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword) + r"\b", message):
                if league_id not in league_ids:
                    league_ids.append(league_id)
                break

    name_filters = []
    for keywords, needle in EXTRA_KEYWORDS:
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword) + r"\b", message):
                if needle not in name_filters:
                    name_filters.append(needle)
                break

    return {
        "start": start,
        "end": end,
        "label": label,
        "target_odds": target_odds,
        "picks": picks,
        "league_ids": league_ids,
        "name_filters": name_filters,
        "risk": risk,
    }


def format_odds(value):
    return str(int(value)) if value == int(value) else str(value)


def describe_request(req):
    chosen = [ALLOWED_LEAGUES[i] for i in req["league_ids"]]
    chosen += [n.title() for n in req["name_filters"]]

    if chosen:
        leagues = ", ".join(chosen)
    else:
        leagues = "all allowed leagues"

    odds = format_odds(req["target_odds"]) if req["target_odds"] else "not set"
    picks = str(req["picks"]) if req["picks"] else "auto"
    risk = {"safe": "ð¡ï¸ Safe", "risky": "ð¥ Risky"}.get(req.get("risk"), "Normal")

    return (
        f"ð Window: {escape(req['label'])}\n"
        f"ð¯ Target odds: {odds}\n"
        f"ð¢ Picks: {picks}\n"
        f"âï¸ Risk: {risk}\n"
        f"ð Leagues: {escape(leagues)}"
    )


# ============================================================
# SCAN FIXTURES
# ============================================================

def dates_in_window(start, end):
    first = start.astimezone(LOCAL_TZ).date()
    last = (end - timedelta(seconds=1)).astimezone(LOCAL_TZ).date()

    dates = []
    current = first
    while current <= last:
        dates.append(current)
        current += timedelta(days=1)

    return dates


def scan_fixtures(start, end, league_ids=None, name_filters=None):
    """Returns (fixtures, errors) for matches that have not started."""
    fixtures = []
    errors = []
    seen_ids = set()
    skipped = False

    for date_value in dates_in_window(start, end):
        date_string = date_value.isoformat()

        # Skip dates the API plan does not allow
        if _plan_range and not (_plan_range[0] <= date_string <= _plan_range[1]):
            skipped = True
            continue

        try:
            daily = get_allowed_fixtures_cached(date_string)
        except Exception as exc:
            if note_plan_limit(str(exc)):
                skipped = True
                continue
            errors.append(f"{date_string}: {exc}")
            continue

        for fixture in daily:
            if league_ids or name_filters:
                league = fixture.get("league", {})
                league_text = (
                    str(league.get("name", "")) + " "
                    + str(league.get("country", ""))
                ).lower()
                by_id = league.get("id") in (league_ids or [])
                by_name = any(n in league_text for n in (name_filters or []))
                if not (by_id or by_name):
                    continue

            status = fixture.get("fixture", {}).get("status", {}).get("short")
            if status != "NS":  # NS = Not Started
                continue

            kickoff = parse_fixture_time(fixture)
            if kickoff is None or kickoff < start or kickoff >= end:
                continue

            fixture_id = fixture.get("fixture", {}).get("id")
            if fixture_id in seen_ids:
                continue
            seen_ids.add(fixture_id)

            fixtures.append(fixture)

    fixtures.sort(key=parse_fixture_time)

    _scan_notes.clear()
    if skipped and plan_note():
        _scan_notes.append(plan_note())

    return fixtures, errors


# ============================================================
# ODDS + PREDICTIONS + TICKET BUILDER
# ============================================================

ANALYZE_LIMIT = 10  # matches analysed per request
ANALYZE_LIMIT_BIG = 14  # used when the target odds are 20 or more
MIN_PICK_PROB = 0.55  # default: ignore picks below 55% estimated chance
MIN_WIN_PROB = 0.55  # default: a team must be this strong to be picked to win
MIN_PICK_ODDS = 1.20  # ignore picks with tiny odds
MODEL_WEIGHT = 0.5  # 50% API-Football prediction, 50% bookmaker odds
MAX_CANDIDATES = 20
MAX_LEGS = 10
MAX_OVERSHOOT = 1.35  # default: ticket odds may be up to 35% above the target
SEARCH_LIMIT = 600000
DEFAULT_PICKS = 5
PREFERRED_BOOKMAKERS = ["bet365", "1xbet"]
ODDS_CACHE_SECONDS = 1800
PREDICTION_CACHE_SECONDS = 10800

_odds_cache = {}
_prediction_cache = {}
_bulk_done = {}  # date_string -> time bulk odds were fetched


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_odds(response):
    """Pick one bookmaker that has Match Winner odds and keep all its markets."""
    if not response:
        return None

    bookmakers = response[0].get("bookmakers", [])

    def rank(bookmaker):
        name = str(bookmaker.get("name", "")).lower()
        for index, preferred in enumerate(PREFERRED_BOOKMAKERS):
            if preferred in name:
                return index
        return 99

    for bookmaker in sorted(bookmakers, key=rank):
        bets = {}
        for bet in bookmaker.get("bets", []):
            name = str(bet.get("name", "")).lower()
            values = {}
            for v in bet.get("values", []):
                odd = _to_float(v.get("odd"))
                if odd:
                    values[str(v.get("value", "")).lower()] = odd
            bets[name] = values

        winner = bets.get("match winner")
        if not winner:
            continue

        home, draw, away = winner.get("home"), winner.get("draw"), winner.get("away")
        if not (home and draw and away):
            continue

        double = bets.get("double chance", {})

        return {
            "bookmaker": bookmaker.get("name", "Bookmaker"),
            "home": home,
            "draw": draw,
            "away": away,
            "dc_1x": double.get("home/draw"),
            "dc_x2": double.get("draw/away"),
            "bets": bets,
        }

    return None


def get_match_odds(fixture_id):
    cached = _odds_cache.get(fixture_id)
    if cached and time.time() - cached[0] < ODDS_CACHE_SECONDS:
        return cached[1]

    result = football_request("odds", {"fixture": fixture_id})
    odds = parse_odds(result.get("response", []))
    _odds_cache[fixture_id] = (time.time(), odds)
    return odds


def fetch_odds_bulk(date_string, wanted_ids, need):
    """One call returns odds for ~10 matches. Fills _odds_cache."""
    done = _bulk_done.get(date_string)
    if done and time.time() - done < ODDS_CACHE_SECONDS:
        return

    page = 1
    while page <= MAX_ODDS_PAGES:
        if _api_remaining is not None and _api_remaining < 4:
            raise BotError("Daily API limit nearly reached.")

        result = football_request("odds", {"date": date_string, "page": page})

        for item in result.get("response", []):
            fixture_id = item.get("fixture", {}).get("id")
            if fixture_id:
                _odds_cache[fixture_id] = (time.time(), parse_odds([item]))

        total = (result.get("paging") or {}).get("total", 1) or 1
        have = sum(
            1 for fid in wanted_ids
            if _odds_cache.get(fid) and _odds_cache[fid][1]
        )
        if page >= total or have >= need:
            break
        page += 1

    _bulk_done[date_string] = time.time()


def get_match_prediction(fixture_id):
    cached = _prediction_cache.get(fixture_id)
    if cached and time.time() - cached[0] < PREDICTION_CACHE_SECONDS:
        return cached[1]

    result = football_request("predictions", {"fixture": fixture_id})
    response = result.get("response", [])

    prediction = None
    if response:
        data = response[0].get("predictions", {}) or {}
        percent = data.get("percent", {}) or {}

        def pct(key):
            value = _to_float(str(percent.get(key, "")).replace("%", ""))
            return value / 100 if value is not None else None

        home, draw, away = pct("home"), pct("draw"), pct("away")
        if None not in (home, draw, away) and (home + draw + away) > 0:
            total = home + draw + away
            prediction = {
                "home": home / total,
                "draw": draw / total,
                "away": away / total,
                "xg_total": None,
                "xg_home": None,
                "xg_away": None,
            }

            # Expected goals from each team's scoring and conceding averages
            teams = response[0].get("teams", {}) or {}

            def goal_avg(team, kind, venue):
                node = teams.get(team, {}).get("league", {}) or {}
                node = (node.get("goals", {}) or {}).get(kind, {}) or {}
                return _to_float((node.get("average", {}) or {}).get(venue))

            home_for = goal_avg("home", "for", "home")
            home_against = goal_avg("home", "against", "home")
            away_for = goal_avg("away", "for", "away")
            away_against = goal_avg("away", "against", "away")

            if None not in (home_for, home_against, away_for, away_against):
                prediction["xg_home"] = (home_for + away_against) / 2
                prediction["xg_away"] = (away_for + home_against) / 2
                prediction["xg_total"] = prediction["xg_home"] + prediction["xg_away"]

    _prediction_cache[fixture_id] = (time.time(), prediction)
    return prediction


def two_way_prob(odd, other_odd):
    """Chance from a two-way market with the bookmaker margin removed."""
    if not odd:
        return None
    if other_odd:
        a, b = 1 / odd, 1 / other_odd
        return a / (a + b)
    return min(0.95 / odd, 0.97)


def find_bet(bets, exact=None, must=(), none_of=()):
    if exact and exact in bets:
        return bets[exact]
    if not must:
        return None
    for name, values in bets.items():
        if all(m in name for m in must) and not any(n in name for n in none_of):
            return values
    return None


def over_lines(values):
    """{'over 9.5': 1.8, 'under 9.5': 1.9} -> {9.5: (1.8, 1.9)}"""
    lines = {}
    for key, odd in (values or {}).items():
        match = re.match(r"^(over|under)\s+(\d+(?:\.\d+)?)$", key.strip())
        if not match:
            continue
        line = float(match.group(2))
        over, under = lines.get(line, (None, None))
        if match.group(1) == "over":
            over = odd
        else:
            under = odd
        lines[line] = (over, under)
    return lines


def build_options(fixture, odds, prediction, min_prob=None, markets=None, min_odds=None):
    """Turn odds + prediction into possible picks for one match."""
    markets = ENABLED_MARKETS if markets is None else markets

    teams = fixture.get("teams", {})
    home_name = teams.get("home", {}).get("name", "Home")
    away_name = teams.get("away", {}).get("name", "Away")
    fixture_id = fixture.get("fixture", {}).get("id")
    bets = odds.get("bets", {})

    # Bookmaker probabilities with the bookmaker margin removed
    inverse = {k: 1 / odds[k] for k in ("home", "draw", "away")}
    total = sum(inverse.values())
    market = {k: v / total for k, v in inverse.items()}

    if prediction:
        probs = {
            k: MODEL_WEIGHT * prediction[k] + (1 - MODEL_WEIGHT) * market[k]
            for k in market
        }
    else:
        probs = market

    options = []
    odds_floor = MIN_PICK_ODDS if min_odds is None else min_odds
    threshold = MIN_PICK_PROB if min_prob is None else min_prob
    win_threshold = MIN_WIN_PROB - (MIN_PICK_PROB - threshold)

    def add(label, prob, odd, from_model, kind, spec):
        if kind not in markets:
            return
        if odd is None or prob is None:
            return
        if odd < odds_floor or prob < threshold:
            return
        options.append({
            "fixture": fixture,
            "fixture_id": fixture_id,
            "label": label,
            "prob": prob,
            "odd": odd,
            "bookmaker": odds["bookmaker"],
            "has_model": from_model,
            "kind": kind,
            "spec": spec,  # what to look for on SportyBet
        })

    # 1) Straight wins, only when the team is strong
    if "win" in markets:
        if probs["home"] >= win_threshold:
            add(f"{home_name} to win", probs["home"], odds["home"],
                prediction is not None, "win", {"kind": "win", "side": "home"})
        if probs["away"] >= win_threshold:
            add(f"{away_name} to win", probs["away"], odds["away"],
                prediction is not None, "win", {"kind": "win", "side": "away"})

    # 2) Over 1.5 goals
    if "goals" in markets:
        goals = find_bet(bets, exact="goals over/under")
        if goals:
            over, under = goals.get("over 1.5"), goals.get("under 1.5")
            p_market = two_way_prob(over, under)
            if p_market is not None:
                spec = {"kind": "goals", "side": "over", "line": 1.5}
                xg = prediction.get("xg_total") if prediction else None
                if xg:
                    p_model = 1 - math.exp(-xg) * (1 + xg)
                    p_over = MODEL_WEIGHT * p_model + (1 - MODEL_WEIGHT) * p_market
                    add("Over 1.5 goals", p_over, over, True, "goals", spec)
                else:
                    add("Over 1.5 goals", p_market, over, False, "goals", spec)

    # 3) Both teams to score (Yes / No)
    if "btts" in markets:
        btts = find_bet(
            bets,
            exact="both teams score",
            must=("both teams", "score"),
            none_of=("1st", "2nd", "first", "second", "half", "result", "/",
                     "&", "+", "total", "win", "draw", "handicap", "odd",
                     "even", "over", "under"),
        )
        if btts:
            yes, no = btts.get("yes"), btts.get("no")
            p_market = two_way_prob(yes, no) if (yes and no) else None
            if p_market is not None:
                xg_home = prediction.get("xg_home") if prediction else None
                xg_away = prediction.get("xg_away") if prediction else None
                if xg_home and xg_away:
                    p_model = (1 - math.exp(-xg_home)) * (1 - math.exp(-xg_away))
                    p_yes = MODEL_WEIGHT * p_model + (1 - MODEL_WEIGHT) * p_market
                    from_model = True
                else:
                    p_yes, from_model = p_market, False
                add("Both teams to score", p_yes, yes, from_model,
                    "btts", {"kind": "btts", "side": "yes"})
                add("Both teams NOT to score", 1 - p_yes, no, from_model,
                    "btts", {"kind": "btts", "side": "no"})

    # 4) Win either half (off by default)
    if "either_half" in markets:
        either = find_bet(bets, must=("either half",), none_of=("both",))
        if either:
            for side, name, key in (("home", home_name, "home"), ("away", away_name, "away")):
                if probs[key] < 0.45:
                    continue
                odd = next((o for k, o in either.items() if side in k), None)
                if odd:
                    add(f"{name} to win either half", min(0.95 / odd, 0.97), odd, False,
                        "either_half", {"kind": "either_half", "side": side})

    # 5) Corners (off by default - many bookies do not list them)
    if "corners" in markets:
        corners = find_bet(
            bets,
            must=("corner",),
            none_of=("home", "away", "1st", "2nd", "first", "second", "half",
                     "race", "handicap", "1x2", "odd", "even"),
        )
        if corners:
            found = []
            for line, (over, under) in over_lines(corners).items():
                prob = two_way_prob(over, under)
                if over and prob and MIN_PICK_ODDS <= over <= 1.9:
                    found.append((prob, line, over))
            for prob, line, over in sorted(found, reverse=True)[:2]:
                add(f"Over {line:g} corners", prob, over, False, "corners",
                    {"kind": "corners", "side": "over", "line": line})

    # 6) Double chance (only in safe mode by default)
    if "dc" in markets:
        add(f"{home_name} or Draw", probs["home"] + probs["draw"], odds["dc_1x"],
            prediction is not None, "dc", {"kind": "dc", "side": "1x"})
        add(f"Draw or {away_name}", probs["draw"] + probs["away"], odds["dc_x2"],
            prediction is not None, "dc", {"kind": "dc", "side": "x2"})

    return options


def analysis_limit(target_odds, picks):
    """How many matches to analyse, and how many strong ones are enough."""
    if target_odds:
        legs = math.log(target_odds) / math.log(1.35)
    elif picks:
        legs = picks
    else:
        legs = DEFAULT_PICKS

    legs = max(1.0, legs)
    limit = max(6, min(ANALYZE_LIMIT_BIG, round(legs * 1.2 + 2)))
    enough = math.ceil(legs) + 2
    return limit, enough


_sporty_events = {}  # fixture_id -> (time, event or None)
SPORTY_EVENT_CACHE_SECONDS = 600


def filter_available(fixture, options, errors, min_odds=None):
    """
    Keep only picks that SportyBet really offers.
    No provider -> picks pass unchanged (the ticket tells you it is unverified).
    Provider set -> match not found, market missing, odds missing, or ANY error
    means the pick is dropped.
    """
    if SPORTYBET_PROVIDER is None or not options:
        return options

    fixture_id = fixture.get("fixture", {}).get("id")
    teams = fixture.get("teams", {})
    home = teams.get("home", {}).get("name", "")
    away = teams.get("away", {}).get("name", "")

    cached = _sporty_events.get(fixture_id)
    if cached and time.time() - cached[0] < SPORTY_EVENT_CACHE_SECONDS:
        event = cached[1]
    else:
        try:
            event = SPORTYBET_PROVIDER.find_event(
                home, away, parse_fixture_time(fixture), league_name_of(fixture)
            )
        except Exception as exc:
            if len(errors) < 2:
                errors.append(f"SportyBet: {exc}")
            return []
        _sporty_events[fixture_id] = (time.time(), event)

    if event is None:
        return []  # match is not on SportyBet at all

    odds_floor = MIN_PICK_ODDS if min_odds is None else min_odds
    kept = []
    for option in options:
        try:
            sporty_odd = SPORTYBET_PROVIDER.get_odds(event, option["spec"])
        except Exception as exc:
            if len(errors) < 2:
                errors.append(f"SportyBet: {exc}")
            continue

        sporty_odd = _to_float(sporty_odd)
        if not sporty_odd or sporty_odd < odds_floor:
            continue  # market missing on SportyBet (or odds too small)

        option = dict(option)
        option["odd"] = sporty_odd  # use SportyBet's own odds
        option["bookmaker"] = "SportyBet"
        option["sporty_event"] = event
        kept.append(option)

    return kept


def analyze_fixtures(fixtures, limit, enough=None, risk="normal"):
    """Bulk-fetch odds by date, then (optionally) a few predictions."""
    profile = RISK_PROFILES.get(risk, RISK_PROFILES["normal"])
    min_prob = profile["min_prob"]
    min_odds = profile["min_odds"]
    markets = profile["markets"] or ENABLED_MARKETS

    ordered = sorted(
        fixtures,
        key=lambda f: (league_priority(f), parse_fixture_time(f)),
    )[:limit]

    result = {
        "options": [],
        "checked": 0,
        "with_odds": 0,
        "no_odds": 0,
        "errors": [],
        "quota_stop": False,
        "dropped": 0,
    }

    wanted = [
        f.get("fixture", {}).get("id") for f in ordered
        if f.get("fixture", {}).get("id")
    ]
    need = enough or len(wanted)

    # 1) Bulk odds: one call per page of ~10 matches
    dates = sorted({
        parse_fixture_time(f).astimezone(LOCAL_TZ).date().isoformat()
        for f in ordered
    })
    for date_string in dates:
        try:
            fetch_odds_bulk(date_string, wanted, need)
        except Exception as exc:
            if len(result["errors"]) < 2:
                result["errors"].append(str(exc))

    fallback_used = 0
    predictions_used = 0

    for fixture in ordered:
        fixture_id = fixture.get("fixture", {}).get("id")
        if not fixture_id:
            continue

        if _api_remaining is not None and _api_remaining < 4:
            result["quota_stop"] = True
            break

        result["checked"] += 1

        cached = _odds_cache.get(fixture_id)
        if cached is None and fallback_used < MAX_SINGLE_FALLBACK:
            # Bulk call did not include this match: try it once on its own
            fallback_used += 1
            try:
                odds = get_match_odds(fixture_id)
            except Exception as exc:
                if len(result["errors"]) < 2:
                    result["errors"].append(str(exc))
                continue
        else:
            odds = cached[1] if cached else None

        if not odds:
            result["no_odds"] += 1
            continue

        result["with_odds"] += 1

        prediction = None
        if (
            USE_PREDICTIONS
            and predictions_used < MAX_PREDICTIONS
            and build_options(fixture, odds, None, min_prob=min_prob - 0.07, markets=markets, min_odds=min_odds)
        ):
            predictions_used += 1
            try:
                prediction = get_match_prediction(fixture_id)
            except Exception as exc:
                if len(result["errors"]) < 2:
                    result["errors"].append(str(exc))

        found = build_options(
            fixture, odds, prediction, min_prob=min_prob, markets=markets, min_odds=min_odds
        )
        kept = filter_available(fixture, found, result["errors"], min_odds=min_odds)
        result["dropped"] += len(found) - len(kept)
        result["options"].extend(kept)

        if enough and len({o["fixture_id"] for o in result["options"]}) >= enough:
            break

    return result


def make_ticket(chosen, target_odds, reached):
    total_odds = 1.0
    win_probability = 1.0

    for option in chosen:
        total_odds *= option["odd"]
        win_probability *= option["prob"]

    chosen = sorted(chosen, key=lambda o: parse_fixture_time(o["fixture"]))

    return {
        "picks": chosen,
        "total_odds": total_odds,
        "win_probability": win_probability,
        "reached": reached,
        "target": target_odds,
    }


def select_ticket(options, target_odds, picks, overshoot=MAX_OVERSHOOT):
    """
    Finds the group of picks (one per match) whose odds multiply to
    the target while keeping the combined chance as high as possible.
    """
    options = sorted(options, key=lambda o: o["prob"], reverse=True)[:MAX_CANDIDATES]
    if not options:
        return None

    # No target odds: just take the safest picks
    if not target_odds:
        wanted = picks or DEFAULT_PICKS
        chosen, used = [], set()
        for option in options:
            if option["fixture_id"] in used:
                continue
            chosen.append(option)
            used.add(option["fixture_id"])
            if len(chosen) >= wanted:
                break
        return make_ticket(chosen, None, True)

    max_legs = picks or MAX_LEGS
    best = {"prob": -1.0, "combo": None}
    nodes = [0]

    def run(limit_factor):
        best["prob"] = -1.0
        best["combo"] = None
        nodes[0] = 0

        def dfs(start, combo, used, odds, prob):
            nodes[0] += 1
            if nodes[0] > SEARCH_LIMIT:
                return

            if combo and odds >= target_odds and (not picks or len(combo) == picks):
                if prob > best["prob"]:
                    best["prob"] = prob
                    best["combo"] = list(combo)

            if len(combo) >= max_legs:
                return

            for index in range(start, len(options)):
                option = options[index]
                if option["fixture_id"] in used:
                    continue

                new_odds = odds * option["odd"]
                if new_odds > target_odds * limit_factor:
                    continue

                combo.append(option)
                used.add(option["fixture_id"])
                dfs(index + 1, combo, used, new_odds, prob * option["prob"])
                combo.pop()
                used.discard(option["fixture_id"])

        dfs(0, [], set(), 1.0, 1.0)

    run(overshoot)
    if best["combo"] is None:
        run(1000.0)

    if best["combo"]:
        return make_ticket(best["combo"], target_odds, True)

    # Could not reach the target: use the best pick of every match
    per_match = {}
    for option in options:
        current = per_match.get(option["fixture_id"])
        if current is None or option["odd"] > current["odd"]:
            per_match[option["fixture_id"]] = option

    chosen = sorted(per_match.values(), key=lambda o: o["prob"], reverse=True)
    chosen = chosen[:max_legs]
    ticket = make_ticket(chosen, target_odds, False)
    return ticket


def confidence_icon(prob):
    if prob >= 0.75:
        return "ð¢"
    if prob >= 0.62:
        return "ð¡"
    return "ð "


def format_ticket(req, ticket, analysis, total_matches):
    lines = ["ð¯ <b>SAMUELBET AI - TICKET</b>"]

    summary = f"ð {escape(req['label'].capitalize())}"
    if req["target_odds"]:
        summary += f" â¢ target odds {format_odds(req['target_odds'])}"
    if req.get("risk") == "safe":
        summary += " â¢ ð¡ï¸ safe mode"
    elif req.get("risk") == "risky":
        summary += " â¢ ð¥ risky mode"
    lines.append(summary)
    lines.append(f"ð Times in {LOCAL_TZ_NAME}")

    if ticket is None:
        min_prob = RISK_PROFILES.get(req.get("risk"), RISK_PROFILES["normal"])["min_prob"]
        lines.append("")
        lines.append(
            "â I could not build a ticket. No analysed match had a strong "
            f"pick (at least {int(min_prob * 100)}% estimated chance)."
        )
    else:
        for number, option in enumerate(ticket["picks"], start=1):
            fixture = option["fixture"]
            kickoff = parse_fixture_time(fixture).astimezone(LOCAL_TZ)
            teams = fixture.get("teams", {})
            home = teams.get("home", {}).get("name", "Home")
            away = teams.get("away", {}).get("name", "Away")

            lines.append("")
            lines.append(
                f"<b>{number}.</b> ð {kickoff.strftime('%a %H:%M')} â¢ "
                f"ð {escape(league_name_of(fixture))}"
            )
            lines.append(f"â½ {escape(home)} vs {escape(away)}")
            lines.append(f"â <b>{escape(option['label'])}</b>")
            lines.append(
                f"ð° Odds {option['odd']:.2f} â¢ "
                f"{confidence_icon(option['prob'])} {option['prob'] * 100:.0f}% chance â¢ "
                f"{'ð' if option['has_model'] else 'ð¦'}"
            )

        lines.append("")
        lines.append("ââââââââââââ")
        lines.append(f"ð° <b>Total odds: {ticket['total_odds']:.2f}</b>")
        lines.append(
            f"ð Estimated chance of all picks winning: "
            f"<b>{ticket['win_probability'] * 100:.0f}%</b>"
        )

        if not ticket["reached"]:
            lines.append("")
            lines.append(
                f"â ï¸ I could not reach {format_odds(ticket['target'])} odds "
                "safely with the matches available. This is the best I found."
            )
            if req.get("risk") == "safe":
                lines.append("Safe mode only uses strong picks, so high odds are hard to reach. "
                             "Try fewer odds or a longer window.")

        bookmakers = sorted({o["bookmaker"] for o in ticket["picks"]})
        lines.append(f"ð¦ Odds from {escape(', '.join(bookmakers))} (yours may differ)")
        if SPORTYBET_PROVIDER is not None:
            lines.append("â Every pick was checked on SportyBet.")
            if ticket.get("booking_code"):
                lines.append(f"ð² SportyBet code: <b>{escape(str(ticket['booking_code']))}</b>")
        else:
            lines.append("ð Not checked on SportyBet yet. Make sure each pick exists there before you stake.")

        lines.append("ð = form + odds   ð¦ = bookmaker odds only")

    lines.append("")
    lines.append(
        f"ð Analysed {analysis['with_odds']} of {total_matches} matches "
        f"({analysis['no_odds']} had no odds)."
    )

    if analysis.get("dropped"):
        lines.append(f"ð« Dropped {analysis['dropped']} pick(s) that SportyBet does not offer.")

    if analysis["quota_stop"]:
        lines.append("â ï¸ Stopped early to save your daily API requests.")

    for error in analysis["errors"]:
        lines.append(f"â ï¸ {escape(error[:200])}")

    for note in _scan_notes:
        lines.append(escape(note))

    if _api_remaining is not None:
        lines.append(f"ð¡ API requests left today: {_api_remaining}")

    lines.append("")
    lines.append(
        "â ï¸ Predictions are estimates, not guarantees. Accumulators lose often. "
        "Only stake what you can afford to lose (18+)."
    )

    return "\n".join(lines)


def prediction_ticket_flow(chat_id, text):
    req = parse_request(text)
    fixtures, errors = scan_fixtures(
        req["start"], req["end"], req["league_ids"], req["name_filters"]
    )

    if not fixtures:
        lines = [
            "ð <b>SamuelBet AI</b>",
            "",
            "<b>Request understood:</b>",
            describe_request(req),
            "",
            "No eligible matches found in this window.",
        ]
        for note in _scan_notes:
            lines.append("")
            lines.append(escape(note))
        if errors:
            lines.append("")
            lines.append("â ï¸ <b>API problem:</b>")
            lines.extend(escape(e) for e in errors)
        send_message(chat_id, "\n".join(lines))
        return

    profile = RISK_PROFILES.get(req["risk"], RISK_PROFILES["normal"])

    limit, enough = analysis_limit(req["target_odds"], req["picks"])
    limit = min(limit + profile["extra_matches"], len(fixtures))

    calls = MAX_ODDS_PAGES * 2 + MAX_SINGLE_FALLBACK + (MAX_PREDICTIONS if USE_PREDICTIONS else 0)
    seconds = max(0, calls - MAX_CALLS_PER_MINUTE) * 60 / MAX_CALLS_PER_MINUTE
    wait_text = "a few seconds" if seconds < 20 else f"up to {int(seconds // 60) + 1} minute(s)"

    send_message(
        chat_id,
        "ð <b>SamuelBet AI</b>\n\n"
        "<b>Request understood:</b>\n"
        f"{describe_request(req)}\n\n"
        f"â³ Analysing {limit} of {len(fixtures)} matches. "
        f"This can take {wait_text}...",
    )

    analysis = analyze_fixtures(fixtures, limit, enough + profile["extra_matches"], req["risk"])
    ticket = select_ticket(
        analysis["options"], req["target_odds"], req["picks"], profile["overshoot"]
    )

    if ticket and SPORTYBET_PROVIDER is not None and hasattr(SPORTYBET_PROVIDER, "create_booking_code"):
        try:
            selections = [(o["sporty_event"], o["spec"]) for o in ticket["picks"]]
            ticket["booking_code"] = SPORTYBET_PROVIDER.create_booking_code(selections)
        except Exception as exc:
            analysis["errors"].append(f"Booking code failed: {exc}")

    send_message(chat_id, format_ticket(req, ticket, analysis, len(fixtures)))


# ============================================================
# MESSAGES
# ============================================================

def fixtures_message(req):
    fixtures, errors = scan_fixtures(req["start"], req["end"], req["league_ids"], req["name_filters"])

    if not fixtures:
        text = (
            "â½ <b>SAMUELBET AI</b>\n\n"
            f"No eligible matches found ({escape(req['label'])}).\n\n"
            "Matches starting in less than 1 hour are skipped."
        )
        for note in _scan_notes:
            text += "\n\n" + escape(note)
        if errors:
            text += "\n\nâ ï¸ <b>API problem:</b>\n"
            text += "\n".join(escape(e) for e in errors)
        return text

    total = len(fixtures)
    # If there are too many, keep your main leagues first
    shown = sorted(
        fixtures,
        key=lambda f: (league_priority(f), parse_fixture_time(f)),
    )[:MAX_FIXTURES_TO_SHOW]

    def sort_key(fixture):
        kickoff = parse_fixture_time(fixture).astimezone(LOCAL_TZ)
        return (
            kickoff.date(),
            league_priority(fixture),
            league_name_of(fixture),
            kickoff,
        )

    shown.sort(key=sort_key)

    lines = [
        "â½ <b>SAMUELBET AI</b>",
        f"ð {escape(req['label'].capitalize())} - times in {LOCAL_TZ_NAME}",
        f"ð {total} eligible match" + ("es" if total != 1 else ""),
    ]

    current_day = None
    current_league = None
    number = 0

    for fixture in shown:
        kickoff = parse_fixture_time(fixture).astimezone(LOCAL_TZ)
        day_label = kickoff.strftime("%A, %d %b %Y")
        league_name = league_name_of(fixture)

        if day_label != current_day:
            current_day = day_label
            current_league = None
            lines.append("")
            lines.append(f"ð <b>{escape(day_label)}</b>")

        if league_name != current_league:
            current_league = league_name
            lines.append("")
            lines.append(f"ð <b>{escape(league_name)}</b>")

        teams = fixture.get("teams", {})
        home = teams.get("home", {}).get("name", "Unknown")
        away = teams.get("away", {}).get("name", "Unknown")

        number += 1
        lines.append(
            f"{number}. {kickoff.strftime('%H:%M')} - "
            f"{escape(home)} vs {escape(away)}"
        )

    if total > len(shown):
        lines.append("")
        lines.append(f"Showing {len(shown)} of {total} matches.")
        lines.append("Narrow it down, for example: /fixtures brazil tonight")

    for note in _scan_notes:
        lines.append("")
        lines.append(escape(note))

    if errors:
        lines.append("")
        lines.append("â ï¸ Some days could not be loaded:")
        lines.extend(escape(e) for e in errors)

    return "\n".join(lines)


def debug_message():
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date().isoformat()
    fixtures = get_fixtures_for_date(today)

    if not fixtures:
        return (
            "ð§ <b>API-FOOTBALL DEBUG</b>\n\n"
            f"Date: {today}\n\n"
            "â The API returned no fixtures for today."
        )

    leagues_today = {}
    for fixture in fixtures:
        league = fixture.get("league", {})
        leagues_today[league.get("id")] = (
            league.get("name"),
            league.get("country"),
        )

    found = [lid for lid in ALLOWED_LEAGUES if lid in leagues_today]
    missing = [lid for lid in ALLOWED_LEAGUES if lid not in leagues_today]

    lines = [
        "ð§ <b>API-FOOTBALL DEBUG</b>",
        f"ð Date: {today}",
        f"â½ Total fixtures: {len(fixtures)}",
        "",
        "â <b>Your leagues playing today:</b>",
    ]

    if found:
        for lid in found:
            lines.append(f"â¢ {escape(ALLOWED_LEAGUES[lid])} (ID {lid})")
    else:
        lines.append("â¢ None")

    extras = sorted(
        (str(name), str(country))
        for lid, (name, country) in leagues_today.items()
        if lid not in ALLOWED_LEAGUES
        and is_extra_competition({"id": lid, "name": name, "country": country})
    )

    lines.append("")
    lines.append("ð <b>International competitions today:</b>")
    if extras:
        for name, country in extras:
            lines.append(f"â¢ {escape(name)}")
    else:
        lines.append("â¢ None")

    lines.append("")
    lines.append("â <b>Your leagues NOT playing today:</b>")

    if missing:
        for lid in missing:
            lines.append(f"â¢ {escape(ALLOWED_LEAGUES[lid])} (ID {lid})")
    else:
        lines.append("â¢ None")

    return "\n".join(lines)


START_TEXT = (
    "â½ <b>Welcome to SamuelBet AI!</b>\n\n"
    "Ask me in simple words.\n\n"
    "<b>Examples:</b>\n"
    "â¢ give me 10 odds for tonight\n"
    "â¢ safe 5 picks tomorrow\n"
    "â¢ what's on in La Liga tomorrow?\n"
    "â¢ make it safer\n"
    "â¢ explain what double chance means\n\n"
    "<b>Commands:</b>\n"
    "/fixtures - upcoming matches\n"
    "/debug - check what the API returns\n"
    "/reset - clear chat memory\n"
    "/help - show help"
)

HELP_TEXT = (
    "â½ <b>SamuelBet AI Help</b>\n\n"
    "<b>Talk to me normally.</b> I understand follow-ups like "
    "\"make it safer\" or \"now do tomorrow\".\n\n"
    "<b>/fixtures</b> shows upcoming matches. Add a window or league:\n"
    "â¢ /fixtures today\n"
    "â¢ /fixtures tomorrow\n"
    "â¢ /fixtures weekend\n"
    "â¢ /fixtures 2 days (the maximum)\n"
    "â¢ /fixtures premier league 5 days\n\n"
    "<b>Tickets:</b>\n"
    "â¢ give me your best 10 odds for tonight\n"
    "â¢ safe 15 odds for today only\n"
    "â¢ risky 30 odds 2 days\n"
    "â¢ 5 picks tomorrow la liga\n"
    "â¢ brazil games tonight\n"
    "â¢ nations league today\n\n"
    "/debug - check which of your leagues play today\n"
    "/reset - clear chat memory\n"
    "/start - start the bot"
)

UNKNOWN_TEXT = (
    "â½ <b>SamuelBet AI</b>\n\n"
    "I didn't understand that yet.\n\n"
    "Try: give me 10 odds tomorrow, safe 5 picks tonight, "
    "or /fixtures today. See /help."
)

PREDICTION_WORDS = [
    "prediction", "predict", "bet", "ticket", "odds", "acca",
    "accumulator", "tonight", "today", "tomorrow", "weekend",
    "match", "matches", "game", "pick", "tip", "day",
]


def looks_like_request(text):
    """True if the text contains one of the words as a whole word."""
    return any(
        re.search(r"\b" + re.escape(word) + r"s?\b", text)
        for word in PREDICTION_WORDS
    )


# ============================================================
# FREE MODE (no AI key needed): small talk + follow-ups
# ============================================================

_last_request = {}  # chat_id -> last ticket request text

WINDOW_RE = r"\b(today|tonight|tomorrow|weekend|week|\d+\s*days?)\b"
ODDS_RE = r"\d+(?:\.\d+)?\s*(?:total\s+)?odds?\b|\bodds?\s*(?:of|:|=)?\s*\d+(?:\.\d+)?"
PICKS_RE = r"\d+\s*(?:picks?|games?|matches|match|selections?|predictions?|tips?|legs?|teams?)\b"

HOW_TO_ASK = (
    "Try: give me 10 odds tomorrow, safe 5 picks tonight, or /fixtures today."
)

SMALLTALK = [
    (r"\b(how are you|how r u|how you dey|how far|how body|wetin dey|what'?s up|whats up|sup)\b",
     "I'm good, thanks for asking! â½ Ready when you are. " + HOW_TO_ASK),
    (r"^\s*(hi|hello|hey|hiya|yo|good (morning|afternoon|evening)|howdy)\b",
     "Hello! â½ " + HOW_TO_ASK),
    (r"\b(thanks|thank you|thx|nice one|well done)\b",
     "You're welcome! â½ Need another ticket? " + HOW_TO_ASK),
    (r"\b(who are you|what can you do|what do you do|your name)\b",
     "I'm SamuelBet AI. I scan upcoming football matches and build tickets for a "
     "target odds. " + HOW_TO_ASK),
]


def smalltalk_reply(text):
    for pattern, reply in SMALLTALK:
        if re.search(pattern, text):
            return reply
    return None


def merge_request(last, new):
    """
    Combine a follow-up like "make it safer" or "tomorrow" with the last request.
    Whatever the new message sets replaces the old value.
    """
    merged = last
    if re.search(WINDOW_RE, new):
        merged = re.sub(WINDOW_RE, " ", merged)
    if re.search(ODDS_RE, new):
        merged = re.sub(ODDS_RE, " ", merged)
    if re.search(PICKS_RE, new):
        merged = re.sub(PICKS_RE, " ", merged)
    if re.search(SAFE_WORDS, new) or re.search(RISKY_WORDS, new):
        merged = re.sub(SAFE_WORDS, " ", merged)
        merged = re.sub(RISKY_WORDS, " ", merged)
    return re.sub(r"\s+", " ", merged + " " + new).strip()


def is_follow_up(chat_id, text):
    """Short message that changes the last request (window, risk or league)."""
    if chat_id not in _last_request:
        return False
    if re.search(ODDS_RE, text) or re.search(PICKS_RE, text):
        return False  # it carries its own numbers: treat as a new request
    if len(text.split()) > 6:
        return False
    return bool(
        re.search(WINDOW_RE, text)
        or re.search(SAFE_WORDS, text)
        or re.search(RISKY_WORDS, text)
    )


# ============================================================
# AI CHAT (Claude understands any message)
# ============================================================

_chat_history = {}  # chat_id -> list of {"role", "content"}


def ai_call(system, messages):
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 700,
        "system": system,
        "messages": messages,
    }).encode("utf-8")

    request = Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:200]
        raise BotError(f"AI HTTP error {exc.code}: {body_text}")
    except URLError as exc:
        raise BotError(f"AI connection error: {exc}")

    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )


def ai_system_prompt():
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    leagues = ", ".join(k[0][0] for k in LEAGUE_KEYWORDS)
    countries = ", ".join(k[0][0] for k in EXTRA_KEYWORDS)

    return f"""You are SamuelBet AI, a friendly football assistant inside a Telegram bot.
The user is in Nigeria. Right now it is {now.strftime('%A %d %B %Y, %H:%M')} {LOCAL_TZ_NAME}.

Reply with ONLY one JSON object, nothing else:
{{"action": "ticket" | "fixtures" | "chat", "request": "...", "reply": "..."}}

ACTIONS
- "ticket": the user wants picks, tips, an accumulator, or a target total odds.
- "fixtures": the user wants to see upcoming matches.
- "chat": everything else (greetings, football talk, how betting or odds work,
  explaining a pick, general questions). Answer fully in "reply" and set "request" to "".

"request" is a short string that the bot's parser reads. Use ONLY these pieces:
- Time window (pick one): today | tonight | tomorrow | weekend | 2 days
- Target total odds: "N odds", for example "20 odds"
- Number of picks: "N picks", for example "5 picks"
- Risk level (optional): safe | risky. Use "safe" when the user asks for safer,
  sure, banker or low risk picks. Use "risky" when they want bolder picks.
- Leagues or countries (optional): {leagues}, {countries}, nations league, afcon, u21
Examples: "10 odds tonight", "safe 5 picks tomorrow premier league", "risky 30 odds 2 days", "brazil today".
A safer ticket really means LOWER TOTAL ODDS (fewer or stronger picks). If the user asks for
safer picks but wants a big odds target, still build it, and say in "reply" that lower total
odds is what makes a ticket safer: a 20 odds ticket wins roughly 1 time in 20 at best.
The bot can only see today and tomorrow. If the user asks for a longer window,
use "2 days" and mention that limit in "reply".

For "ticket" and "fixtures", "reply" is ONE short friendly line, for example
"Sure, building a safe 10 odds ticket for tonight."
Use the conversation history for follow-ups like "make it tomorrow", "make it safer"
or "now only La Liga": copy the earlier request and change only what the user asked.

The bot cannot do corners, cards, or other special markets. It only uses match winner,
over 1.5 goals, both teams to score (yes or no), and (in safe mode) double chance.
If the user asks for something else, say so in a chat reply instead of pretending.

STYLE
- Casual, warm, short. Plain text only, no markdown, no HTML.
- You have no live scores, injuries or news. Never invent results or stats.
- Be honest that no pick is guaranteed and accumulators lose often.
  Do not pressure anyone to bet. Mention staking responsibly only when it fits.
- Reply in the language the user writes in."""


def parse_ai_json(text):
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def ai_decide(chat_id, text):
    """Ask the AI what the user wants. Returns a dict."""
    history = _chat_history.setdefault(chat_id, [])
    messages = history + [{"role": "user", "content": text}]

    raw = ai_call(ai_system_prompt(), messages)
    data = parse_ai_json(raw)
    if data is None:
        data = {"action": "chat", "request": "", "reply": raw.strip()}
        raw = json.dumps(data)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": raw})
    del history[:-MAX_HISTORY]

    return data


def ai_dispatch(chat_id, text, data):
    action = str(data.get("action", "chat")).lower()
    reply = str(data.get("reply", "")).strip()
    request = str(data.get("request", "")).strip()

    if action == "ticket":
        if reply:
            send_message(chat_id, escape(reply))
        prediction_ticket_flow(chat_id, request or text.lower())
    elif action == "fixtures":
        if reply:
            send_message(chat_id, escape(reply))
        send_message(chat_id, fixtures_message(parse_request(request or "2 days")))
    else:
        send_message(chat_id, escape(reply or "I'm here. What do you want to know?"))


# ============================================================
# HANDLE MESSAGES
# ============================================================

def handle_text(chat_id, text):
    message = text.strip()
    lowered = message.lower()

    if lowered.startswith("/"):
        parts = lowered.split(None, 1)
        command = parts[0].split("@")[0]
        args = parts[1] if len(parts) > 1 else ""
    else:
        command = ""
        args = ""

    if command == "/start":
        send_message(chat_id, START_TEXT)
        return

    if command == "/help":
        send_message(chat_id, HELP_TEXT)
        return

    if command == "/reset":
        _chat_history.pop(chat_id, None)
        _last_request.pop(chat_id, None)
        send_message(chat_id, "ð§¹ Chat memory cleared.")
        return

    if command == "/debug":
        try:
            send_message(chat_id, debug_message())
        except Exception as exc:
            send_message(chat_id, f"â Debug error:\n{escape(str(exc))}")
        return

    if command == "/fixtures":
        try:
            req = parse_request(args if args else "2 days")
            send_message(chat_id, fixtures_message(req))
        except Exception as exc:
            send_message(chat_id, f"â Fixture error:\n{escape(str(exc))}")
        return

    if command == "/ticket":
        try:
            prediction_ticket_flow(chat_id, args if args else lowered)
        except Exception as exc:
            send_message(chat_id, f"â Error:\n{escape(str(exc))}")
        return

    # ---- normal chat: let the AI understand it ----
    ai_problem = None
    if not command and ANTHROPIC_API_KEY:
        decision = None
        try:
            telegram_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            decision = ai_decide(chat_id, message)
        except Exception as exc:
            ai_problem = str(exc)[:200]
            print(f"AI error: {exc}")

        if decision is not None:
            try:
                ai_dispatch(chat_id, message, decision)
            except Exception as exc:
                send_message(chat_id, f"â Error:\n{escape(str(exc))}")
            return

    # ---- free mode (no AI key, or the AI failed) ----
    if not command:
        # Greetings first, unless the message also asks for something
        asks_for_something = (
            re.search(ODDS_RE, lowered) or re.search(PICKS_RE, lowered)
            or re.search(WINDOW_RE, lowered) or re.search(SAFE_WORDS, lowered)
            or re.search(RISKY_WORDS, lowered)
        )
        chat_reply = smalltalk_reply(lowered)
        if chat_reply and not asks_for_something:
            send_message(chat_id, escape(chat_reply, quote=False))
            return

        request_text = None
        if is_follow_up(chat_id, lowered):
            request_text = merge_request(_last_request[chat_id], lowered)
        elif looks_like_request(lowered):
            request_text = lowered

        if request_text:
            _last_request[chat_id] = request_text
            try:
                prediction_ticket_flow(chat_id, request_text)
            except Exception as exc:
                send_message(chat_id, f"â Error:\n{escape(str(exc))}")
            return

    if ai_problem:
        send_message(
            chat_id,
            "ð My AI chat is not working right now, so I can only do football "
            "requests.\n\n"
            f"<b>Problem:</b> {escape(ai_problem)}\n\n"
            + escape(HOW_TO_ASK),
        )
        return

    send_message(chat_id, UNKNOWN_TEXT)



# ============================================================
# FOOTBALL-DATA.ORG OVERRIDES
# ============================================================
# The original project was written around API-Football.  SportyTips now
# uses football-data.org v4 for football evidence.  These definitions are
# deliberately placed after the legacy helpers so every existing caller
# keeps the same public function names while using the new provider.

_fd_call_times = []
_FD_MAX_CALLS_PER_MINUTE = 9


def _fd_throttle():
    now = time.time()
    while _fd_call_times and now - _fd_call_times[0] > 60:
        _fd_call_times.pop(0)
    if len(_fd_call_times) >= _FD_MAX_CALLS_PER_MINUTE:
        wait = 60 - (now - _fd_call_times[0]) + 0.25
        if wait > 0:
            time.sleep(wait)
    _fd_call_times.append(time.time())


def football_request(endpoint, params=None, retries=2):
    """Call football-data.org v4 using the private Render environment token."""
    if not FOOTBALL_DATA_API_KEY:
        raise BotError("Football data service is not configured.")

    clean = str(endpoint or "").strip().lstrip("/")
    url = f"{FOOTBALL_DATA_BASE_URL}/{clean}"
    if params:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url += "?" + query

    last_error = None
    for attempt in range(retries + 1):
        _fd_throttle()
        request = Request(
            url,
            method="GET",
            headers={
                "X-Auth-Token": FOOTBALL_DATA_API_KEY,
                "User-Agent": "SportyTips/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=API_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"football-data HTTP {exc.code}"
            if exc.code == 429 and attempt < retries:
                time.sleep(10)
                continue
            if exc.code in (401, 403):
                raise BotError("Football data service rejected the configured token.")
            raise BotError(last_error)
        except URLError:
            last_error = "football-data connection error"
            if attempt < retries:
                time.sleep(2)
                continue
            raise BotError(last_error)
        except (ValueError, json.JSONDecodeError):
            raise BotError("Football data service returned invalid data.")

    raise BotError(last_error or "Football data request failed.")


def get_fixtures_for_date(date_string):
    result = football_request(
        "matches",
        {"dateFrom": date_string, "dateTo": date_string},
    )
    if not isinstance(result, dict):
        return []
    return result.get("matches", []) or []


def get_allowed_fixtures_cached(date_string):
    cached = _fixture_cache.get(date_string)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    fixtures = get_fixtures_for_date(date_string)
    clean = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        competition = fixture.get("competition") or {}
        name = str(competition.get("name") or "").lower()
        # Women's matches are excluded by default. Smart ticket also has
        # its own competition-name safety filter.
        if "women" in name or "women's" in name:
            continue
        clean.append(fixture)

    _fixture_cache[date_string] = (time.time(), clean)
    if len(_fixture_cache) > 20:
        oldest = min(_fixture_cache, key=lambda k: _fixture_cache[k][0])
        _fixture_cache.pop(oldest, None)
    return clean


def football_api_status():
    """Internal compatibility status; never shown to users."""
    return {
        "provider": "football-data.org",
        "configured": bool(FOOTBALL_DATA_API_KEY),
        "requests_per_minute": 10,
    }


# ============================================================
# MAIN LOOP
# ============================================================

def get_updates(offset=None):
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset

    result = telegram_request("getUpdates", params)
    return result.get("result", [])


def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return

    if not FOOTBALL_DATA_API_KEY:
        print("ERROR: FOOTBALL_DATA_API_KEY is missing.")
        return

    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY is missing. AI chat is off, keyword mode only.")

    print("SamuelBet AI is running.")
    offset = None

    while True:
        try:
            updates = get_updates(offset)

            for update in updates:
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message:
                    continue

                chat_id = message.get("chat", {}).get("id")
                text = message.get("text")

                if not chat_id or not text:
                    continue

                handle_text(chat_id, text)

        except KeyboardInterrupt:
            print("SamuelBet AI stopped.")
            break

        except Exception as exc:
            print(f"Bot error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
