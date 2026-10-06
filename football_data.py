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
    - API-Football prediction data
    - H2H
    - Poisson goal model
    - evidence-based market probability

Persistent cache:
    - Upstash Redis REST API when configured
    - local SQLite fallback when Redis is not configured

The persistent cache has no user-entered limit.
Old entries naturally expire according to their TTL.
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


# ============================================================
# SETTINGS
# ============================================================

ENRICH_MAX = int(os.getenv("ENRICH_MAX", "10"))
ENRICH_SECONDS = int(os.getenv("ENRICH_SECONDS", "100"))
MIN_QUOTA = int(os.getenv("MIN_QUOTA", "1"))

# Facts can safely live for several hours.
FACTS_CACHE_SECONDS = int(
    os.getenv("FACTS_CACHE_SECONDS", str(6 * 3600))
)

# Fixture lists change more often.
FIXTURE_CACHE_SECONDS = int(
    os.getenv("FIXTURE_CACHE_SECONDS", str(15 * 60))
)

# Persistent cache entries for predictions.
PREDICTION_CACHE_SECONDS = int(
    os.getenv("PREDICTION_CACHE_SECONDS", str(3 * 3600))
)

# Cache version.
# Change this when the prediction model changes substantially.
CACHE_VERSION = os.getenv("FOOTBALL_CACHE_VERSION", "v4")

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

REDIS_URL = (
    os.getenv("UPSTASH_REDIS_REST_URL")
    or os.getenv("REDIS_URL")
    or ""
).strip().rstrip("/")

REDIS_TOKEN = (
    os.getenv("UPSTASH_REDIS_REST_TOKEN")
    or os.getenv("REDIS_TOKEN")
    or ""
).strip()


def _redis_enabled():
    """
    Upstash REST is preferred.

    We deliberately use urllib instead of requiring another Python
    package, so deployment is simpler on Render.
    """
    return bool(
        REDIS_URL
        and REDIS_TOKEN
        and (
            "upstash.io" in REDIS_URL
            or REDIS_URL.startswith("https://")
        )
    )


def _redis_request(command):
    """
    Execute one Redis command through Upstash REST.

    Returns None on failure so football prediction can continue
    using memory/local fallback rather than crashing.
    """
    if not _redis_enabled():
        return None

    try:
        url = REDIS_URL

        if not url.endswith("/"):
            url += "/"

        encoded = "/".join(
            urllib.parse.quote(str(x), safe="")
            for x in command
        )

        request = urllib.request.Request(
            url + encoded,
            headers={
                "Authorization": "Bearer " + REDIS_TOKEN,
                "Content-Type": "application/json",
            },
            method="GET",
        )

        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")

        data = json.loads(raw)
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
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        result = _redis_request(
            ["SET", key, payload, "EX", int(ttl)]
        )

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

CACHE_DB = os.getenv(
    "FOOTBALL_CACHE_DB",
    "/tmp/samuelbet_football_cache.sqlite3",
)


def _sqlite_connect():
    connection = sqlite3.connect(
        CACHE_DB,
        timeout=15,
        check_same_thread=False,
    )

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

        row = connection.execute(
            """
            SELECT value, expires_at
            FROM football_cache
            WHERE cache_key = ?
            """,
            (key,),
        ).fetchone()

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
            INSERT INTO football_cache
            (cache_key, value, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key)
            DO UPDATE SET
                value = excluded.value,
                expires_at = excluded.expires_at,
                created_at = excluded.created_at
            """,
            (
                key,
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now + int(ttl),
                now,
            ),
        )

        connection.commit()
        connection.close()

        return True

    except Exception:
        return False


def _sqlite_delete(key):
    try:
        connection = _sqlite_connect()

        connection.execute(
            "DELETE FROM football_cache WHERE cache_key = ?",
            (key,),
        )

        connection.commit()
        connection.close()

        return True

    except Exception:
        return False


# ============================================================
# UNIFIED CACHE
# ============================================================

def _cache_get(key):
    """
    Memory → Redis → SQLite.

    Redis is preferred because it survives Render restarts/redeploys.
    """

    now = time.time()

    item = _facts_cache.get(key)

    if item:
        expires, value = item

        if expires > now:
            return value

        _facts_cache.pop(key, None)

    value = _redis_get(key)

    if value is not None:
        _facts_cache[key] = (
            now + min(FACTS_CACHE_SECONDS, 300),
            value,
        )
        return value

    value = _sqlite_get(key)

    if value is not None:
        _facts_cache[key] = (
            now + min(FACTS_CACHE_SECONDS, 300),
            value,
        )
        return value

    return None


def _cache_set(key, value, ttl):
    """
    Save to memory plus persistent cache.
    """

    _facts_cache[key] = (
        time.time() + min(int(ttl), 300),
        value,
    )

    if _redis_enabled():
        _redis_set(key, value, ttl)

    # SQLite is also maintained as a fallback.
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
    "manchester united": [
        "man utd",
        "man united",
        "manchester utd",
        "manchester united",
        "man united fc",
        "manchester united fc",
    ],
    "manchester city": [
        "man city",
        "manchester city",
        "manchester city fc",
    ],
    "tottenham hotspur": [
        "tottenham",
        "spurs",
        "tottenham hotspur",
        "tottenham hotspur fc",
    ],
    "newcastle united": [
        "newcastle",
        "newcastle united",
        "newcastle utd",
    ],
    "west ham united": [
        "west ham",
        "west ham united",
        "west ham utd",
    ],
    "wolverhampton wanderers": [
        "wolves",
        "wolverhampton",
        "wolverhampton wanderers",
    ],
    "brighton and hove albion": [
        "brighton",
        "brighton and hove albion",
        "brighton hove albion",
    ],
    "nottingham forest": [
        "nottingham forest",
        "nottm forest",
        "forest",
    ],
    "crystal palace": [
        "crystal palace",
        "palace",
    ],
    "aston villa": [
        "aston villa",
        "villa",
    ],
    "afc bournemouth": [
        "bournemouth",
        "afc bournemouth",
    ],
    "leicester city": [
        "leicester",
        "leicester city",
    ],
    "ipswich town": [
        "ipswich",
        "ipswich town",
    ],
    "leeds united": [
        "leeds",
        "leeds united",
        "leeds utd",
    ],
    "everton": [
        "everton",
        "everton fc",
    ],
    "liverpool": [
        "liverpool",
        "liverpool fc",
    ],
    "arsenal": [
        "arsenal",
        "arsenal fc",
    ],
    "chelsea": [
        "chelsea",
        "chelsea fc",
    ],
    "fulham": [
        "fulham",
        "fulham fc",
    ],
    "brentford": [
        "brentford",
        "brentford fc",
    ],
    "burnley": [
        "burnley",
        "burnley fc",
    ],
    "sunderland": [
        "sunderland",
        "sunderland afc",
    ],

    # Spain
    "real madrid": [
        "real madrid",
        "real madrid cf",
    ],
    "barcelona": [
        "barcelona",
        "fc barcelona",
    ],
    "atletico madrid": [
        "atletico madrid",
        "atl madrid",
        "atletico",
    ],
    "sevilla": [
        "sevilla",
        "sevilla fc",
    ],
    "real sociedad": [
        "real sociedad",
    ],
    "athletic club": [
        "athletic bilbao",
        "athletic club",
    ],
    "villarreal": [
        "villarreal",
        "villarreal cf",
    ],
    "real betis": [
        "real betis",
        "betis",
    ],
    "valencia": [
        "valencia",
        "valencia cf",
    ],
    "girona": [
        "girona",
        "girona fc",
    ],

    # Germany
    "bayern munich": [
        "bayern",
        "bayern munich",
        "fc bayern munich",
    ],
    "borussia dortmund": [
        "dortmund",
        "borussia dortmund",
        "bvb",
    ],
    "rb leipzig": [
        "rb leipzig",
        "leipzig",
    ],
    "bayer leverkusen": [
        "bayer leverkusen",
        "leverkusen",
    ],
    "eintracht frankfurt": [
        "eintracht frankfurt",
        "frankfurt",
    ],

    # Italy
    "inter milan": [
        "inter",
        "inter milan",
        "internazionale",
    ],
    "ac milan": [
        "ac milan",
        "milan",
    ],
    "juventus": [
        "juventus",
        "juve",
    ],
    "napoli": [
        "napoli",
        "ssc napoli",
    ],
    "roma": [
        "roma",
        "as roma",
    ],
    "lazio": [
        "lazio",
        "ss lazio",
    ],
    "atalanta": [
        "atalanta",
        "atalanta bc",
    ],

    # France
    "paris saint germain": [
        "psg",
        "paris saint-germain",
        "paris saint germain",
        "paris sg",
    ],
    "marseille": [
        "marseille",
        "olympique marseille",
    ],
    "lyon": [
        "lyon",
        "olympique lyon",
    ],
    "monaco": [
        "monaco",
        "as monaco",
    ],
    "lille": [
        "lille",
        "losc lille",
    ],

    # Netherlands
    "ajax": [
        "ajax",
        "afc ajax",
    ],
    "psv eindhoven": [
        "psv",
        "psv eindhoven",
    ],
    "feyenoord": [
        "feyenoord",
    ],

    # Portugal
    "benfica": [
        "benfica",
        "sl benfica",
    ],
    "porto": [
        "porto",
        "fc porto",
    ],
    "sporting cp": [
        "sporting",
        "sporting lisbon",
        "sporting cp",
    ],

    # Turkey
    "galatasaray": [
        "galatasaray",
        "galatasaray sk",
    ],
    "fenerbahce": [
        "fenerbahce",
        "fenerbahçe",
        "fenerbahce sk",
    ],
    "besiktas": [
        "besiktas",
        "beşiktaş",
    ],

    # Scotland
    "celtic": [
        "celtic",
        "celtic fc",
    ],
    "rangers": [
        "rangers",
        "rangers fc",
    ],
}


def _normalise_team(value):
    if value is None:
        return ""

    text = unicodedata.normalize(
        "NFKD",
        str(value),
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = re.sub(
        r"\b(fc|afc|cf|sc|sk|fk|club|football club)\b",
        " ",
        text,
    )

    text = re.sub(r"[^a-z0-9]+", " ", text)

    text = re.sub(r"\s+", " ", text).strip()

    for canonical, aliases in TEAM_ALIASES.items():
        names = [canonical] + aliases

        for alias in names:
            alias_n = re.sub(
                r"[^a-z0-9]+",
                " ",
                alias.lower(),
            )

            alias_n = re.sub(
                r"\s+",
                " ",
                alias_n,
            ).strip()

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

    return SequenceMatcher(
        None,
        aa,
        bb,
    ).ratio()


# ============================================================
# FIXTURE HELPERS
# ============================================================

def _fixture_teams(fixture):
    """
    Read home/away names from several common API-Football shapes.
    """

    if not isinstance(fixture, dict):
        return "", ""

    teams = fixture.get("teams") or {}

    home = (
        (teams.get("home") or {}).get("name")
        or fixture.get("home")
        or fixture.get("homeTeam")
        or fixture.get("home_name")
        or ""
    )

    away = (
        (teams.get("away") or {}).get("name")
        or fixture.get("away")
        or fixture.get("awayTeam")
        or fixture.get("away_name")
        or ""
    )

    return str(home), str(away)


def _fixture_timestamp(fixture):
    if not isinstance(fixture, dict):
        return None

    fixture_data = fixture.get("fixture") or {}

    value = (
        fixture_data.get("date")
        or fixture.get("date")
        or fixture.get("kickoff")
        or fixture.get("startTime")
        or fixture.get("start_time")
    )

    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value,
                timezone.utc,
            )
        except Exception:
            return None

    text = str(value).strip()

    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def _event_kickoff(event):
    if not isinstance(event, dict):
        return None

    values = [
        event.get("kickoff"),
        event.get("startTime"),
        event.get("start_time"),
        event.get("date"),
        event.get("eventDate"),
        event.get("commence_time"),
        event.get("commenceTime"),
    ]

    for value in values:
        if not value:
            continue

        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(
                    value,
                    timezone.utc,
                )
            except Exception:
                continue

        text = str(value).strip()

        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"

            dt = datetime.fromisoformat(text)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc)

        except Exception:
            pass

    return None


def _event_teams(event):
    if not isinstance(event, dict):
        return "", ""

    home = (
        event.get("home")
        or event.get("homeTeam")
        or event.get("home_team")
        or event.get("home_name")
        or ""
    )

    away = (
        event.get("away")
        or event.get("awayTeam")
        or event.get("away_team")
        or event.get("away_name")
        or ""
    )

    if isinstance(home, dict):
        home = home.get("name") or ""

    if isinstance(away, dict):
        away = away.get("name") or ""

    return str(home), str(away)


# ============================================================
# FOOTBALL FIXTURE RETRIEVAL
# ============================================================

def _football_request(endpoint, params=None):
    """
    Use the API-Football request helper already exposed by main.py.

    Different versions of the project have used slightly different
    helper signatures, so we try the common forms.
    """

    params = params or {}

    fn = getattr(bot, "football_request", None)

    if not callable(fn):
        return None

    attempts = [
        lambda: fn(endpoint, params),
        lambda: fn(endpoint=endpoint, params=params),
        lambda: fn(endpoint, **params),
    ]

    for attempt in attempts:
        try:
            result = attempt()

            if result is not None:
                return result

        except TypeError:
            continue

        except Exception:
            return None

    return None


def _extract_response_list(response):
    if response is None:
        return []

    if isinstance(response, list):
        return response

    if isinstance(response, dict):
        response_list = response.get("response")

        if isinstance(response_list, list):
            return response_list

        data = response.get("data")

        if isinstance(data, list):
            return data

        results = response.get("results")

        if isinstance(results, list):
            return results

    return []


def _fixtures_for(date_string):
    """
    First use the existing SportyTips football fixture cache/helper.

    If that fails or returns nothing, ask API-Football directly.

    This fallback is important because the previous 159 SportyBet /
    0 football matches problem can happen when the internal fixture
    list is empty.
    """

    cache_key = (
        f"football-fixtures:{CACHE_VERSION}:{date_string}"
    )

    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    fixtures = []

    # Existing project helper.
    try:
        helper = getattr(
            bot,
            "get_allowed_fixtures_cached",
            None,
        )

        if callable(helper):
            result = helper(date_string)

            if isinstance(result, list):
                fixtures.extend(result)

            elif isinstance(result, dict):
                fixtures.extend(
                    _extract_response_list(result)
                )

    except Exception:
        pass

    # Direct API-Football fallback.
    if not fixtures:
        response = _football_request(
            "fixtures",
            {
                "date": date_string,
            },
        )

        fixtures.extend(
            _extract_response_list(response)
        )

    # Remove duplicates.
    unique = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue

        fixture_id = (
            (fixture.get("fixture") or {}).get("id")
            or fixture.get("id")
        )

        if fixture_id is not None:
            unique[str(fixture_id)] = fixture
        else:
            home, away = _fixture_teams(fixture)
            key = (
                _normalise_team(home)
                + "|"
                + _normalise_team(away)
            )

            unique[key] = fixture

    fixtures = list(unique.values())

    _cache_set(
        cache_key,
        fixtures,
        FIXTURE_CACHE_SECONDS,
    )

    return fixtures


def _candidate_dates(event):
    kickoff = _event_kickoff(event)

    if kickoff is None:
        now = datetime.now(timezone.utc)
        base = now.date()
    else:
        base = kickoff.date()

    return [
        base,
        base - timedelta(days=1),
        base + timedelta(days=1),
    ]


def _match_score(event, fixture):
    eh, ea = _event_teams(event)
    fh, fa = _fixture_teams(fixture)

    if not eh or not ea or not fh or not fa:
        return 0.0

    normal = (
        _team_similarity(eh, fh)
        + _team_similarity(ea, fa)
    ) / 2

    reversed_score = (
        _team_similarity(eh, fa)
        + _team_similarity(ea, fh)
    ) / 2

    score = max(normal, reversed_score)

    event_dt = _event_kickoff(event)
    fixture_dt = _fixture_timestamp(fixture)

    if event_dt and fixture_dt:
        difference = abs(
            (event_dt - fixture_dt).total_seconds()
        )

        # 15 minutes or less: excellent.
        if difference <= 15 * 60:
            score += 0.12

        # Up to 2 hours: still useful.
        elif difference <= 2 * 3600:
            score += 0.08

        # Same calendar day.
        elif event_dt.date() == fixture_dt.date():
            score += 0.04

        # Different day and large time difference.
        else:
            score -= 0.05

    return min(score, 1.20)


def find_fixture(event):
    """
    Match a SportyBet event to a real football fixture.

    Returns the actual API-Football fixture dictionary or None.
    """

    if not event:
        return None

    home, away = _event_teams(event)

    if not home or not away:
        return None

    event_dt = _event_kickoff(event)

    cache_key = (
        "fixture-match:"
        + CACHE_VERSION
        + ":"
        + _normalise_team(home)
        + ":"
        + _normalise_team(away)
        + ":"
        + (
            event_dt.strftime("%Y-%m-%d-%H")
            if event_dt
            else "unknown"
        )
    )

    cached = _cache_get(cache_key)

    if cached:
        return cached

    candidates = []

    for date_value in _candidate_dates(event):
        date_string = date_value.strftime("%Y-%m-%d")

        for fixture in _fixtures_for(date_string):
            score = _match_score(
                event,
                fixture,
            )

            if score >= 0.72:
                candidates.append(
                    (
                        score,
                        fixture,
                    )
                )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score, best_fixture = candidates[0]

    # Require strong team identity.
    if best_score < 0.82:
        return None

    _cache_set(
        cache_key,
        best_fixture,
        FIXTURE_CACHE_SECONDS,
    )

    return best_fixture


# ============================================================
# FACT EXTRACTION
# ============================================================

def _safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default

        return float(value)

    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default

        return int(float(value))

    except Exception:
        return default


def _goals_from_fixture(fixture):
    goals = fixture.get("goals") or {}

    home = _safe_int(
        goals.get("home"),
        0,
    )

    away = _safe_int(
        goals.get("away"),
        0,
    )

    return home, away


def _fixture_team_name(fixture, side):
    teams = fixture.get("teams") or {}
    team = teams.get(side) or {}

    return team.get("name") or ""


def _extract_form_from_prediction(prediction):
    """
    API-Football prediction endpoint often returns:
        predictions.form.home
        predictions.form.away

    Form strings may contain W/D/L.
    """

    if not isinstance(prediction, dict):
        return "", ""

    predictions = prediction.get("predictions") or {}

    form = predictions.get("form") or {}

    home = (
        form.get("home")
        or prediction.get("home_form")
        or ""
    )

    away = (
        form.get("away")
        or prediction.get("away_form")
        or ""
    )

    return str(home), str(away)


def _count_form(form):
    text = str(form or "").upper()

    return {
        "W": text.count("W"),
        "D": text.count("D"),
        "L": text.count("L"),
    }


def _extract_percentages(prediction):
    predictions = (
        prediction.get("predictions")
        if isinstance(prediction, dict)
        else {}
    ) or {}

    percent = (
        predictions.get("percent")
        or prediction.get("percent")
        or {}
    ) or {}

    home = _safe_float(
        percent.get("home"),
    )

    draw = _safe_float(
        percent.get("draw"),
    )

    away = _safe_float(
        percent.get("away"),
    )

    def normalise(value):
        if value is None:
            return None

        if value > 1:
            return value / 100.0

        return value

    return (
        normalise(home),
        normalise(draw),
        normalise(away),
    )


def _extract_lambda(prediction, side):
    """
    Attempt to read expected-goal values from API-Football.
    """

    if not isinstance(prediction, dict):
        return None

    predictions = prediction.get("predictions") or {}

    candidates = [
        predictions.get(
            f"expected_goals_{side}"
        ),
        predictions.get(
            f"expectedGoals{side.title()}"
        ),
        predictions.get(
            f"lambda_{side}"
        ),
        prediction.get(
            f"expected_goals_{side}"
        ),
        prediction.get(
            f"lambda_{side}"
        ),
    ]

    for value in candidates:
        number = _safe_float(value)

        if number is not None and 0 < number < 8:
            return number

    return None


def _extract_prediction_fixture_id(prediction):
    if not isinstance(prediction, dict):
        return None

    fixture = prediction.get("fixture") or {}

    return (
        fixture.get("id")
        or prediction.get("fixture_id")
        or prediction.get("id")
    )


def _h2h_results(home, away):
    """
    Retrieve H2H using API-Football.

    Returns a list of fixtures.
    """

    response = _football_request(
        "fixtures/headtohead",
        {
            "h2h": (
                f"{home}-{away}"
            ),
            "last": 10,
        },
    )

    return _extract_response_list(response)


def _recent_team_results(team_id, last=5):
    if not team_id:
        return []

    response = _football_request(
        "fixtures",
        {
            "team": team_id,
            "last": last,
        },
    )

    return _extract_response_list(response)


def _team_id_from_fixture(fixture, side):
    teams = fixture.get("teams") or {}
    team = teams.get(side) or {}

    return (
        team.get("id")
        or fixture.get(f"{side}_id")
    )


def _calculate_recent_stats(
    results,
    team_id,
):
    wins = 0
    draws = 0
    losses = 0

    scored = 0
    conceded = 0

    count = 0

    for fixture in results or []:
        if not isinstance(fixture, dict):
            continue

        teams = fixture.get("teams") or {}
        goals = fixture.get("goals") or {}

        home = teams.get("home") or {}
        away = teams.get("away") or {}

        home_id = home.get("id")
        away_id = away.get("id")

        if home_id != team_id and away_id != team_id:
            continue

        hg = _safe_int(
            goals.get("home"),
            0,
        )

        ag = _safe_int(
            goals.get("away"),
            0,
        )

        count += 1

        if home_id == team_id:
            scored += hg
            conceded += ag

            if hg > ag:
                wins += 1
            elif hg == ag:
                draws += 1
            else:
                losses += 1

        else:
            scored += ag
            conceded += hg

            if ag > hg:
                wins += 1
            elif ag == hg:
                draws += 1
            else:
                losses += 1

    if count == 0:
        return {
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "scored": 0,
            "conceded": 0,
            "avg_scored": None,
            "avg_conceded": None,
        }

    return {
        "played": count,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "scored": scored,
        "conceded": conceded,
        "avg_scored": scored / count,
        "avg_conceded": conceded / count,
    }


def _venue_stats(results, team_id, venue):
    wins = 0
    draws = 0
    losses = 0
    scored = 0
    conceded = 0
    played = 0

    for fixture in results or []:
        teams = fixture.get("teams") or {}
        goals = fixture.get("goals") or {}

        home = teams.get("home") or {}
        away = teams.get("away") or {}

        home_id = home.get("id")
        away_id = away.get("id")

        if team_id not in (
            home_id,
            away_id,
        ):
            continue

        is_home = home_id == team_id

        if venue == "home" and not is_home:
            continue

        if venue == "away" and is_home:
            continue

        hg = _safe_int(
            goals.get("home"),
            0,
        )

        ag = _safe_int(
            goals.get("away"),
            0,
        )

        played += 1

        if is_home:
            scored += hg
            conceded += ag

            if hg > ag:
                wins += 1
            elif hg == ag:
                draws += 1
            else:
                losses += 1

        else:
            scored += ag
            conceded += hg

            if ag > hg:
                wins += 1
            elif ag == hg:
                draws += 1
            else:
                losses += 1

    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "scored": scored,
        "conceded": conceded,
    }


def _parse_h2h(h2h, home_id, away_id):
    home_wins = 0
    away_wins = 0
    draws = 0

    home_scored = 0
    away_scored = 0

    played = 0

    for fixture in h2h or []:
        teams = fixture.get("teams") or {}
        goals = fixture.get("goals") or {}

        fh = (teams.get("home") or {}).get("id")
        fa = (teams.get("away") or {}).get("id")

        hg = _safe_int(
            goals.get("home"),
            0,
        )

        ag = _safe_int(
            goals.get("away"),
            0,
        )

        if {
            fh,
            fa,
        } != {
            home_id,
            away_id,
        }:
            continue

        played += 1

        if fh == home_id:
            home_scored += hg
            away_scored += ag

            if hg > ag:
                home_wins += 1
            elif hg == ag:
                draws += 1
            else:
                away_wins += 1

        else:
            home_scored += ag
            away_scored += hg

            if ag > hg:
                home_wins += 1
            elif ag == hg:
                draws += 1
            else:
                away_wins += 1

    return {
        "played": played,
        "home_wins": home_wins,
        "draws": draws,
        "away_wins": away_wins,
        "home_scored": home_scored,
        "away_scored": away_scored,
    }


def parse_facts(
    fixture,
    prediction=None,
    h2h=None,
    home_recent=None,
    away_recent=None,
):
    """
    Convert API-Football information into a compact facts object.
    """

    prediction = prediction or {}
    h2h = h2h or []

    home = _fixture_team_name(
        fixture,
        "home",
    )

    away = _fixture_team_name(
        fixture,
        "away",
    )

    home_id = _team_id_from_fixture(
        fixture,
        "home",
    )

    away_id = _team_id_from_fixture(
        fixture,
        "away",
    )

    home_form, away_form = _extract_form_from_prediction(
        prediction
    )

    home_form_counts = _count_form(
        home_form
    )

    away_form_counts = _count_form(
        away_form
    )

    prediction_home, prediction_draw, prediction_away = (
        _extract_percentages(prediction)
    )

    if home_recent is None:
        home_recent = []

    if away_recent is None:
        away_recent = []

    home_recent_stats = _calculate_recent_stats(
        home_recent,
        home_id,
    )

    away_recent_stats = _calculate_recent_stats(
        away_recent,
        away_id,
    )

    home_venue = _venue_stats(
        home_recent,
        home_id,
        "home",
    )

    away_venue = _venue_stats(
        away_recent,
        away_id,
        "away",
    )

    h2h_stats = _parse_h2h(
        h2h,
        home_id,
        away_id,
    )

    home_lambda = _extract_lambda(
        prediction,
        "home",
    )

    away_lambda = _extract_lambda(
        prediction,
        "away",
    )

    # Build reasonable Poisson lambdas if API-Football does not provide
    # expected goals directly.
    if home_lambda is None:
        components = []

        if home_recent_stats["avg_scored"] is not None:
            components.append(
                home_recent_stats["avg_scored"]
            )

        if away_recent_stats["avg_conceded"] is not None:
            components.append(
                away_recent_stats["avg_conceded"]
            )

        if components:
            home_lambda = sum(components) / len(
                components
            )

    if away_lambda is None:
        components = []

        if away_recent_stats["avg_scored"] is not None:
            components.append(
                away_recent_stats["avg_scored"]
            )

        if home_recent_stats["avg_conceded"] is not None:
            components.append(
                home_recent_stats["avg_conceded"]
            )

        if components:
            away_lambda = sum(components) / len(
                components
            )

    if home_lambda is not None:
        home_lambda = max(
            0.10,
            min(home_lambda, 5.0),
        )

    if away_lambda is not None:
        away_lambda = max(
            0.10,
            min(away_lambda, 5.0),
        )

    return {
        "home": home,
        "away": away,

        "home_id": home_id,
        "away_id": away_id,

        "form": {
            "home": home_form,
            "away": away_form,
            "home_counts": home_form_counts,
            "away_counts": away_form_counts,
        },

        "recent": {
            "home": home_recent_stats,
            "away": away_recent_stats,
        },

        "venue": {
            "home": home_venue,
            "away": away_venue,
        },

        "prediction": {
            "home": prediction_home,
            "draw": prediction_draw,
            "away": prediction_away,
        },

        "h2h": h2h_stats,

        "lambda": {
            "home": home_lambda,
            "away": away_lambda,
        },

        "fixture_id": (
            (fixture.get("fixture") or {}).get("id")
            or fixture.get("id")
        ),

        "retrieved_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# GET FACTS
# ============================================================

def get_facts(fixture):
    """
    Get football facts for a real fixture.

    Persistent cache is checked first.
    """

    if not fixture:
        return None

    fixture_id = (
        (fixture.get("fixture") or {}).get("id")
        or fixture.get("id")
    )

    home = _fixture_team_name(
        fixture,
        "home",
    )

    away = _fixture_team_name(
        fixture,
        "away",
    )

    if fixture_id:
        identifier = str(fixture_id)
    else:
        identifier = (
            _normalise_team(home)
            + "-"
            + _normalise_team(away)
        )

    cache_key = (
        "football-facts:"
        + CACHE_VERSION
        + ":"
        + identifier
    )

    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    # --------------------------------------------------------
    # Prediction
    # --------------------------------------------------------

    prediction = None

    if fixture_id:
        response = _football_request(
            "predictions",
            {
                "fixture": fixture_id,
            },
        )

        prediction_items = _extract_response_list(
            response
        )

        if prediction_items:
            prediction = prediction_items[0]

    if prediction is None:
        prediction = {}

    # --------------------------------------------------------
    # Recent form
    # --------------------------------------------------------

    home_id = _team_id_from_fixture(
        fixture,
        "home",
    )

    away_id = _team_id_from_fixture(
        fixture,
        "away",
    )

    home_recent = _recent_team_results(
        home_id,
        last=5,
    )

    away_recent = _recent_team_results(
        away_id,
        last=5,
    )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = _h2h_results(
        home_id,
        away_id,
    ) if home_id and away_id else []

    facts = parse_facts(
        fixture,
        prediction=prediction,
        h2h=h2h,
        home_recent=home_recent,
        away_recent=away_recent,
    )

    _cache_set(
        cache_key,
        facts,
        FACTS_CACHE_SECONDS,
    )

    return facts


# ============================================================
# FACTS DIGEST
# ============================================================

def facts_digest(facts):
    if not facts:
        return "No football facts available."

    home = facts.get("home") or "Home"
    away = facts.get("away") or "Away"

    recent = facts.get("recent") or {}
    venue = facts.get("venue") or {}
    prediction = facts.get("prediction") or {}
    h2h = facts.get("h2h") or {}
    lambdas = facts.get("lambda") or {}

    h_recent = recent.get("home") or {}
    a_recent = recent.get("away") or {}

    h_venue = venue.get("home") or {}
    a_venue = venue.get("away") or {}

    return (
        f"{home}: "
        f"last 5 "
        f"{h_recent.get('wins', 0)}W/"
        f"{h_recent.get('draws', 0)}D/"
        f"{h_recent.get('losses', 0)}L, "
        f"scored {h_recent.get('scored', 0)}, "
        f"conceded {h_recent.get('conceded', 0)}. "
        f"Home record "
        f"{h_venue.get('wins', 0)}W/"
        f"{h_venue.get('draws', 0)}D/"
        f"{h_venue.get('losses', 0)}L. "
        f"{away}: "
        f"last 5 "
        f"{a_recent.get('wins', 0)}W/"
        f"{a_recent.get('draws', 0)}D/"
        f"{a_recent.get('losses', 0)}L, "
        f"scored {a_recent.get('scored', 0)}, "
        f"conceded {a_recent.get('conceded', 0)}. "
        f"Away record "
        f"{a_venue.get('wins', 0)}W/"
        f"{a_venue.get('draws', 0)}D/"
        f"{a_venue.get('losses', 0)}L. "
        f"Model percentages "
        f"H={prediction.get('home')}, "
        f"D={prediction.get('draw')}, "
        f"A={prediction.get('away')}. "
        f"H2H: "
        f"{h2h.get('home_wins', 0)} home wins, "
        f"{h2h.get('draws', 0)} draws, "
        f"{h2h.get('away_wins', 0)} away wins. "
        f"Expected goals "
        f"{lambdas.get('home')} - "
        f"{lambdas.get('away')}."
    )


# ============================================================
# POISSON
# ============================================================

def _poisson_pmf(k, lam):
    if lam is None or lam <= 0:
        return 0.0

    try:
        return math.exp(
            -lam
            + k * math.log(lam)
            - math.lgamma(k + 1)
        )
    except Exception:
        return 0.0


def _poisson_cdf(k, lam):
    if lam is None or lam < 0:
        return 0.0

    total = 0.0

    for i in range(
        int(k) + 1
    ):
        total += _poisson_pmf(
            i,
            lam,
        )

    return max(
        0.0,
        min(1.0, total),
    )


def _score_probability(
    home_lambda,
    away_lambda,
    condition,
):
    if (
        home_lambda is None
        or away_lambda is None
    ):
        return None

    total = 0.0

    for home_goals in range(0, 11):
        for away_goals in range(0, 11):

            p = (
                _poisson_pmf(
                    home_goals,
                    home_lambda,
                )
                * _poisson_pmf(
                    away_goals,
                    away_lambda,
                )
            )

            if condition(
                home_goals,
                away_goals,
            ):
                total += p

    return max(
        0.0,
        min(1.0, total),
    )


# ============================================================
# MARKET CLASSIFICATION
# ============================================================

def _candidate_text(candidate):
    if not isinstance(candidate, dict):
        return ""

    return " ".join(
        str(
            candidate.get(key)
            or ""
        )
        for key in (
            "market",
            "selection",
            "name",
            "label",
            "type",
        )
    ).lower()


def _win_like(candidate):
    text = _candidate_text(candidate)

    return any(
        value in text
        for value in (
            "1up",
            "2up",
            "home win",
            "away win",
            "straight win",
            "to win",
            "either half",
        )
    )


def _goals_like(candidate):
    text = _candidate_text(candidate)

    return any(
        value in text
        for value in (
            "over 1.5",
            "over 2.5",
            "over 3.5",
            "over 0.5",
            "team goals",
            "total goals",
            "btts",
            "both teams to score",
        )
    )


def _dc_like(candidate):
    text = _candidate_text(candidate)

    return any(
        value in text
        for value in (
            "double chance",
            "draw no bet",
            "dnb",
        )
    )


def _is_under(candidate):
    return "under" in _candidate_text(candidate)


def _is_yellow_card(candidate):
    text = _candidate_text(candidate)

    return any(
        value in text
        for value in (
            "yellow card",
            "yellow cards",
            "booking",
            "bookings",
        )
    )


def _is_negative_handicap(candidate):
    text = _candidate_text(candidate)

    # Explicitly reject negative handicap lines.
    return bool(
        re.search(
            r"handicap[^0-9\-]*-\s*\d",
            text,
        )
        or re.search(
            r"\-\s*1(?:\.25|\.5|\.75)?",
            text,
        )
        or re.search(
            r"\-\s*2(?:\.25|\.5|\.75)?",
            text,
        )
    )


# ============================================================
# FOOTBALL-ONLY PROBABILITY MODEL
# ============================================================

def _clamp(value, low=0.0, high=0.995):
    try:
        value = float(value)
    except Exception:
        return low

    return max(
        low,
        min(high, value),
    )


def _blend(values, weights):
    usable = []

    for value, weight in zip(
        values,
        weights,
    ):
        if value is None:
            continue

        usable.append(
            (
                float(value),
                float(weight),
            )
        )

    if not usable:
        return None

    total_weight = sum(
        weight
        for _, weight in usable
    )

    if total_weight <= 0:
        return None

    return sum(
        value * weight
        for value, weight in usable
    ) / total_weight


def _form_win_rate(stats):
    if not stats:
        return None

    played = stats.get("played", 0)

    if not played:
        return None

    return (
        stats.get("wins", 0)
        / played
    )


def _venue_win_rate(stats):
    if not stats:
        return None

    played = stats.get("played", 0)

    if not played:
        return None

    return (
        stats.get("wins", 0)
        / played
    )


def _poisson_home_win(
    home_lambda,
    away_lambda,
):
    return _score_probability(
        home_lambda,
        away_lambda,
        lambda h, a: h > a,
    )


def _poisson_away_win(
    home_lambda,
    away_lambda,
):
    return _score_probability(
        home_lambda,
        away_lambda,
        lambda h, a: a > h,
    )


def _poisson_draw(
    home_lambda,
    away_lambda,
):
    return _score_probability(
        home_lambda,
        away_lambda,
        lambda h, a: h == a,
    )


def _poisson_over(
    home_lambda,
    away_lambda,
    line,
):
    return _score_probability(
        home_lambda,
        away_lambda,
        lambda h, a: (
            h + a
        ) > line,
    )


def _poisson_btts(
    home_lambda,
    away_lambda,
):
    return _score_probability(
        home_lambda,
        away_lambda,
        lambda h, a: (
            h >= 1
            and a >= 1
        ),
    )


def _poisson_home_team_over(
    home_lambda,
    line,
):
    if home_lambda is None:
        return None

    return _clamp(
        1.0 - _poisson_cdf(
            math.floor(line),
            home_lambda,
        )
    )


def _poisson_away_team_over(
    away_lambda,
    line,
):
    if away_lambda is None:
        return None

    return _clamp(
        1.0 - _poisson_cdf(
            math.floor(line),
            away_lambda,
        )
    )


def _football_win_probability(
    facts,
    side,
):
    if not facts:
        return None

    recent = facts.get("recent") or {}
    venue = facts.get("venue") or {}
    prediction = facts.get("prediction") or {}
    h2h = facts.get("h2h") or {}
    lambdas = facts.get("lambda") or {}

    if side == "home":
        form = _form_win_rate(
            recent.get("home")
        )

        venue_rate = _venue_win_rate(
            venue.get("home")
        )

        api_rate = prediction.get(
            "home"
        )

        poisson = _poisson_home_win(
            lambdas.get("home"),
            lambdas.get("away"),
        )

        h2h_played = h2h.get(
            "played",
            0,
        )

        h2h_rate = None

        if h2h_played:
            h2h_rate = (
                h2h.get("home_wins", 0)
                / h2h_played
            )

    else:
        form = _form_win_rate(
            recent.get("away")
        )

        venue_rate = _venue_win_rate(
            venue.get("away")
        )

        api_rate = prediction.get(
            "away"
        )

        poisson = _poisson_away_win(
            lambdas.get("home"),
            lambdas.get("away"),
        )

        h2h_played = h2h.get(
            "played",
            0,
        )

        h2h_rate = None

        if h2h_played:
            h2h_rate = (
                h2h.get("away_wins", 0)
                / h2h_played
            )

    # Football data only.
    #
    # API prediction:
    #   35%
    #
    # Recent form:
    #   25%
    #
    # Venue:
    #   20%
    #
    # Poisson:
    #   15%
    #
    # H2H:
    #   5%
    #
    # H2H is intentionally small because old H2H should not
    # dominate current team strength.
    return _blend(
        [
            api_rate,
            form,
            venue_rate,
            poisson,
            h2h_rate,
        ],
        [
            0.35,
            0.25,
            0.20,
            0.15,
            0.05,
        ],
    )


# ============================================================
# CANDIDATE PROBABILITY
# ============================================================

def _extract_line(candidate):
    text = _candidate_text(candidate)

    matches = re.findall(
        r"(\d+(?:\.\d+)?)",
        text,
    )

    if not matches:
        return None

    try:
        return float(matches[-1])
    except Exception:
        return None


def _positive_handicap_probability(
    facts,
    candidate,
):
    """
    Approximate positive Asian handicap probability
    from the football score distribution.

    We do NOT use SportyBet implied probability.
    """

    lambdas = facts.get("lambda") or {}

    hl = lambdas.get("home")
    al = lambdas.get("away")

    if hl is None or al is None:
        return None

    text = _candidate_text(candidate)

    line = _extract_line(candidate)

    if line is None:
        return None

    # Determine team.
    home_pick = (
        "home" in text
        or facts.get("home", "").lower()
        in text
    )

    away_pick = (
        "away" in text
        or facts.get("away", "").lower()
        in text
    )

    if not home_pick and not away_pick:
        return None

    probability = 0.0

    for hg in range(0, 11):
        for ag in range(0, 11):

            score = (
                _poisson_pmf(hg, hl)
                * _poisson_pmf(ag, al)
            )

            if home_pick:
                margin = hg - ag
            else:
                margin = ag - hg

            # Positive handicap >= +0.5.
            if line >= 0.5:
                if margin + line > 0:
                    probability += score

            else:
                # +0 / 0 is effectively a push/win line.
                if margin + line > 0:
                    probability += score
                elif margin + line == 0:
                    probability += score * 0.5

    return _clamp(probability)


def adjusted_p(candidate, facts):
    """
    Calculate probability from football evidence.

    IMPORTANT:
    candidate['p'] is intentionally NOT used as the starting
    probability.

    SportyBet odds are therefore not allowed to manufacture
    football confidence.
    """

    if not candidate or not facts:
        return SURE_P_NODATA

    text = _candidate_text(candidate)

    # --------------------------------------------------------
    # Forbidden markets
    # --------------------------------------------------------

    if _dc_like(candidate):
        return 0.0

    if _is_under(candidate):
        return 0.0

    if _is_yellow_card(candidate):
        return 0.0

    if _is_negative_handicap(candidate):
        return 0.0

    # --------------------------------------------------------
    # 1X2 / 1UP / 2UP
    # --------------------------------------------------------

    if _win_like(candidate):

        home_prob = _football_win_probability(
            facts,
            "home",
        )

        away_prob = _football_win_probability(
            facts,
            "away",
        )

        if (
            home_prob is None
            and away_prob is None
        ):
            return SURE_P_NODATA

        if (
            "away" in text
            and away_prob is not None
        ):
            probability = away_prob

        elif (
            "home" in text
            and home_prob is not None
        ):
            probability = home_prob

        else:
            probability = max(
                home_prob or 0,
                away_prob or 0,
            )

        # 1UP is treated as a strong win-type selection.
        if "1up" in text:
            probability *= 0.96

        # 2UP requires a stronger margin.
        if "2up" in text:
            probability *= 0.88

        # Either-half win is less demanding than full match,
        # but still football-modelled.
        if "either half" in text:
            probability = min(
                0.95,
                probability + 0.10,
            )

        return _clamp(
            probability,
            0.0,
            0.97,
        )

    # --------------------------------------------------------
    # Goals / BTTS
    # --------------------------------------------------------

    if _goals_like(candidate):

        lambdas = facts.get("lambda") or {}

        hl = lambdas.get("home")
        al = lambdas.get("away")

        if hl is None or al is None:
            return SURE_P_NODATA

        if (
            "btts" in text
            or "both teams" in text
        ):
            probability = _poisson_btts(
                hl,
                al,
            )

            return (
                _clamp(probability)
                if probability is not None
                else SURE_P_NODATA
            )

        if "over" in text:

            line = _extract_line(candidate)

            if line is None:
                return SURE_P_NODATA

            probability = _poisson_over(
                hl,
                al,
                line,
            )

            return (
                _clamp(probability)
                if probability is not None
                else SURE_P_NODATA
            )

        # Team goals.
        if (
            "team goals" in text
            or "home goals" in text
            or "away goals" in text
        ):
            line = _extract_line(candidate)

            if line is None:
                return SURE_P_NODATA

            if (
                "away" in text
                and "home" not in text
            ):
                probability = (
                    _poisson_away_team_over(
                        al,
                        line,
                    )
                )
            else:
                probability = (
                    _poisson_home_team_over(
                        hl,
                        line,
                    )
                )

            return (
                _clamp(probability)
                if probability is not None
                else SURE_P_NODATA
            )

    # --------------------------------------------------------
    # Positive handicap
    # --------------------------------------------------------

    if "handicap" in text:
        probability = _positive_handicap_probability(
            facts,
            candidate,
        )

        if probability is not None:
            return probability

    # --------------------------------------------------------
    # Corners
    # --------------------------------------------------------

    if "corner" in text:
        # We deliberately do NOT pretend goals are corner data.
        #
        # Unless corner statistics have been fetched and placed
        # in facts['corners'], this market receives no evidence.
        corners = facts.get("corners")

        if not corners:
            return SURE_P_NODATA

        probability = corners.get(
            "probability"
        )

        if probability is not None:
            return _clamp(probability)

        return SURE_P_NODATA

    return SURE_P_NODATA


# ============================================================
# MARKET REASON
# ============================================================

def reason_for(
    candidate,
    facts,
    home=None,
    away=None,
):
    if not facts:
        return (
            "No football data was available for this match."
        )

    home = (
        home
        or facts.get("home")
        or "Home"
    )

    away = (
        away
        or facts.get("away")
        or "Away"
    )

    text = _candidate_text(candidate)

    recent = facts.get("recent") or {}
    venue = facts.get("venue") or {}
    lambdas = facts.get("lambda") or {}

    h_recent = recent.get("home") or {}
    a_recent = recent.get("away") or {}

    h_venue = venue.get("home") or {}
    a_venue = venue.get("away") or {}

    if "corner" in text:
        return (
            "Corner data was not used unless actual corner "
            "statistics were available."
        )

    if (
        "btts" in text
        or "both teams" in text
    ):
        return (
            f"{home} and {away} have a football-modelled "
            f"BTTS probability based on expected-goal "
            f"rates of {lambdas.get('home')} and "
            f"{lambdas.get('away')}."
        )

    if "over" in text:
        return (
            f"The football model estimates expected goals "
            f"of {lambdas.get('home')} for {home} and "
            f"{lambdas.get('away')} for {away}, supporting "
            f"the selected Over market."
        )

    if (
        "1up" in text
        or "2up" in text
        or "win" in text
    ):
        return (
            f"{home}: {h_recent.get('wins', 0)} wins in "
            f"the latest {h_recent.get('played', 0)} "
            f"available matches, with a home record of "
            f"{h_venue.get('wins', 0)} wins from "
            f"{h_venue.get('played', 0)}. "
            f"{away}: {a_recent.get('wins', 0)} wins in "
            f"the latest {a_recent.get('played', 0)} "
            f"available matches, with an away record of "
            f"{a_venue.get('wins', 0)} wins from "
            f"{a_venue.get('played', 0)}."
        )

    return facts_digest(facts)


# ============================================================
# PASTED TICKET HELPERS
# ============================================================

def event_for(leg):
    if not isinstance(leg, dict):
        return None

    return (
        leg.get("event")
        or leg.get("fixture")
        or leg.get("match")
        or leg
    )


def classify_leg(leg):
    """
    Legacy classifier retained for compatibility.

    Active ticket generation should still reject:
        - DNB
        - Under
        - Double chance
        - Yellow cards
        - Negative handicaps
    """

    text = _candidate_text(
        leg if isinstance(leg, dict) else {}
    )

    if "draw no bet" in text or "dnb" in text:
        return "dnb"

    if "double chance" in text:
        return "double_chance"

    if "under" in text:
        return "under"

    if "yellow card" in text:
        return "yellow_cards"

    if "corner" in text:
        return "corners"

    if "btts" in text:
        return "btts"

    if "handicap" in text:
        return "handicap"

    if "1up" in text:
        return "1up"

    if "2up" in text:
        return "2up"

    if "over" in text:
        return "over"

    if "win" in text:
        return "win"

    return "other"


def why(leg, facts=None):
    if not facts:
        return (
            "No football evidence available."
        )

    home = facts.get("home")
    away = facts.get("away")

    return reason_for(
        leg,
        facts,
        home,
        away,
    )


# ============================================================
# RANKING
# ============================================================

def _rank(candidate, facts):
    probability = adjusted_p(
        candidate,
        facts,
    )

    if probability <= 0:
        return -1

    # Small evidence bonus.
    recent = facts.get("recent") or {}

    played = (
        (recent.get("home") or {}).get(
            "played",
            0,
        )
        +
        (recent.get("away") or {}).get(
            "played",
            0,
        )
    )

    evidence_bonus = min(
        0.04,
        played / 250.0,
    )

    return min(
        0.99,
        probability + evidence_bonus,
    )


def safest_for_leg(
    leg,
    facts=None,
):
    if not facts:
        return None

    probability = adjusted_p(
        leg,
        facts,
    )

    if probability <= 0:
        return None

    result = dict(leg)

    result["football_probability"] = (
        probability
    )

    result["reason"] = reason_for(
        result,
        facts,
        facts.get("home"),
        facts.get("away"),
    )

    return result


def rebuild_safer(
    legs,
    facts_by_event=None,
):
    """
    Rebuild a ticket using football evidence.

    No-data selections are not substituted with bookmaker
    confidence.
    """

    facts_by_event = facts_by_event or {}

    output = []

    for leg in legs or []:
        event = event_for(leg)

        event_key = (
            str(
                event.get("id")
                or event.get("eventId")
                or event.get("fixture_id")
                or ""
            )
            if isinstance(event, dict)
            else ""
        )

        facts = facts_by_event.get(
            event_key
        )

        if not facts:
            continue

        candidate = safest_for_leg(
            leg,
            facts,
        )

        if not candidate:
            continue

        output.append(candidate)

    output.sort(
        key=lambda item: item.get(
            "football_probability",
            0,
        ),
        reverse=True,
    )

    return output


# ============================================================
# CACHE CONTROL
# ============================================================

def clear_cache():
    """
    Clear in-memory cache.

    Persistent entries are intentionally NOT wiped by default.

    This means restarting the app does not destroy your saved
    football knowledge.
    """

    _facts_cache.clear()
    _fixture_index.clear()
    _prediction_cache.clear()


def clear_persistent_cache():
    """
    Explicitly clear persistent cache.

    This is NOT called automatically.
    """

    try:
        connection = _sqlite_connect()

        connection.execute(
            "DELETE FROM football_cache"
        )

        connection.commit()
        connection.close()

    except Exception:
        pass

    # We cannot efficiently FLUSH the entire Redis database
    # because that could destroy unrelated application data.
    #
    # Versioning is therefore the safe Redis invalidation method.
    #
    # Increase FOOTBALL_CACHE_VERSION when you intentionally
    # want to invalidate the football cache.


# ============================================================
# DIAGNOSTICS
# ============================================================

def diag_summary():
    """
    Internal diagnostics only.

    Do not expose this directly in Telegram/frontend responses.
    """

    return {
        "cache_version": CACHE_VERSION,
        "redis_enabled": _redis_enabled(),
        "memory_entries": len(_facts_cache),
        "fixture_entries": len(_fixture_index),
        "prediction_entries": len(_prediction_cache),
        "persistent_store": (
            "upstash"
            if _redis_enabled()
            else "sqlite"
        ),
        "facts_ttl": FACTS_CACHE_SECONDS,
        "fixture_ttl": FIXTURE_CACHE_SECONDS,
        "prediction_ttl": PREDICTION_CACHE_SECONDS,
    }