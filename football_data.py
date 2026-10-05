"""
Football evidence engine for SportyTips.

IMPORTANT:
    This module is evidence-first.

    OLD:
        SportyBet odds -> implied probability -> small data adjustment

    NEW:
        Football evidence -> football probability -> best market -> odds check

Football evidence currently includes:
    - recent form
    - recent goals scored/conceded
    - home/away record
    - head-to-head
    - API-Football's football prediction percentages
    - Poisson goal model

SportyBet odds are NOT used to create the football probability.

API usage:
    /predictions = one request per studied fixture
    Results are cached for 3 hours.

The free API-Football plan is limited, so ENRICH_MAX and ENRICH_SECONDS
control how many matches can be studied during one ticket build.
"""

import math
import os
import re
import time
from datetime import datetime, timezone

import main as bot
import sportybet_provider as sp


# ============================================================
# SETTINGS
# ============================================================

ENRICH_MAX = int(os.getenv("ENRICH_MAX", "10"))
ENRICH_SECONDS = int(os.getenv("ENRICH_SECONDS", "45"))

# Keep some API requests in reserve.
MIN_QUOTA = 6

FACTS_CACHE_SECONDS = 3 * 3600

# SportyBet market floor.
MIN_ODDS = 1.30

# These are football-evidence thresholds.
#
# IMPORTANT:
# These are NOT "guaranteed" probabilities.
# They are model probabilities.
SURE_P_DATA = 0.70
SURE_P_NODATA = 0.66

# How strongly the different evidence sources influence the model.
FORM_WEIGHT = 0.30
GOALS_WEIGHT = 0.25
VENUE_WEIGHT = 0.15
H2H_WEIGHT = 0.10
API_MODEL_WEIGHT = 0.20


_facts_cache = {}
_fixture_index = {}
_diag = {}


# ============================================================
# DIAGNOSTICS
# ============================================================

def reset_diag():
    _diag.clear()


def _clean(message):
    """Keep error text readable and hide technical service names."""
    text = re.sub(
        r"API[- ]?Football",
        "the football data service",
        str(message),
        flags=re.I,
    )

    text = text.replace(
        "Daily API limit reached",
        "the daily request limit is used up",
    )

    text = re.sub(
        r"plan only allows",
        "plan permits only",
        text,
        flags=re.I,
    )

    text = re.sub(r"\bAPI\b", "data service", text)

    return text.replace("\n", " ")[:140]


def _note(key, detail=""):
    entry = _diag.setdefault(
        key,
        {
            "n": 0,
            "detail": "",
        },
    )

    entry["n"] += 1

    if detail and not entry["detail"]:
        entry["detail"] = _clean(detail)


def diag_summary():
    """Return a human-readable explanation when football data is unavailable."""

    parts = []

    if "fixtures_error" in _diag:
        parts.append(
            "the football data service would not give me the match list "
            f"({_diag['fixtures_error']['detail']})"
        )

    if "no_fixtures" in _diag:
        parts.append(
            "the football data service has no matches listed for that day"
        )

    if "quota" in _diag:
        parts.append(
            "the daily request limit of the football data service is almost used up"
        )

    if "no_match" in _diag:
        count = _diag["no_match"]["n"]
        parts.append(
            f"{count} match{'es' if count > 1 else ''} could not be matched "
            "to the football data service"
        )

    if "facts_error" in _diag:
        parts.append(
            "the football data service refused the form request "
            f"({_diag['facts_error']['detail']})"
        )

    if "no_data" in _diag:
        parts.append(
            "the football data service has no usable form or head-to-head data"
        )

    return "; ".join(parts)


NO_DATA = (
    "There is not enough football data for this match, so I will not pretend "
    "the model has high confidence."
)


# ============================================================
# FIND MATCH IN API-FOOTBALL
# ============================================================

def _fixtures_for(date_string):
    """
    Get API-Football fixtures for a date.

    The fixture list itself is cached by main.py.
    This second cache prevents repeated matching work.
    """

    cached = _fixture_index.get(date_string)

    if cached and time.time() - cached[0] < 600:
        return cached[1]

    try:
        fixtures = bot.get_allowed_fixtures_cached(date_string)

    except Exception as exc:
        print(f"Fixture list for {date_string} failed: {exc}")

        _note(
            "fixtures_error",
            str(exc),
        )

        fixtures = []

    _fixture_index[date_string] = (
        time.time(),
        fixtures,
    )

    return fixtures


def find_fixture(event):
    """
    Match a SportyBet event to the corresponding API-Football fixture.

    Matching uses:
        - kickoff time
        - home team name
        - away team name
    """

    if not event:
        return None

    ms = event.get("estimateStartTime")

    if not ms:
        return None

    try:
        kickoff = datetime.fromtimestamp(
            ms / 1000,
            tz=timezone.utc,
        )
    except Exception:
        return None

    date_string = (
        kickoff
        .astimezone(bot.LOCAL_TZ)
        .date()
        .isoformat()
    )

    home = event.get("homeTeamName", "")
    away = event.get("awayTeamName", "")

    best = None
    best_score = 0.0

    listed = _fixtures_for(date_string)

    if not listed and "fixtures_error" not in _diag:
        _note("no_fixtures")

    for fixture in listed:

        stamp = (
            fixture.get("fixture") or {}
        ).get("timestamp")

        if stamp:
            # Allow a fairly wide window because different providers
            # can sometimes store kickoff times slightly differently.
            if abs(stamp - kickoff.timestamp()) > 45 * 60:
                continue

        teams = fixture.get("teams") or {}

        api_home = (
            teams.get("home") or {}
        ).get("name", "")

        api_away = (
            teams.get("away") or {}
        ).get("name", "")

        try:
            home_score = sp._sim(
                home,
                api_home,
            )

            away_score = sp._sim(
                away,
                api_away,
            )

            score = (
                home_score +
                away_score
            ) / 2

        except Exception:
            score = 0.0

        if score > best_score:
            best = fixture
            best_score = score

    if best_score < 0.72:

        if listed:
            _note("no_match")

        return None

    return best


# ============================================================
# BASIC NUMBER HELPERS
# ============================================================

def _num(value):
    try:
        return float(
            str(value)
            .replace("%", "")
            .strip()
        )
    except (TypeError, ValueError):
        return None


def _clamp(value, low=0.02, high=0.97):
    return max(
        low,
        min(high, float(value)),
    )


def _safe_average(values):
    values = [
        float(v)
        for v in values
        if v is not None
    ]

    if not values:
        return None

    return sum(values) / len(values)


# ============================================================
# PARSE API-FOOTBALL FACTS
# ============================================================

def parse_facts(item):
    """
    Convert an API-Football /predictions response into compact
    football evidence.

    No bookmaker odds are used here.
    """

    if not item:
        return None

    teams = item.get("teams") or {}

    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}

    # --------------------------------------------------------
    # RECENT FORM
    # --------------------------------------------------------

    def form5(team):
        form = (
            (team.get("league") or {})
            .get("form")
        )

        if not form:
            return ""

        return str(form)[-5:]

    # --------------------------------------------------------
    # RECENT GOALS
    # --------------------------------------------------------

    def last5_goal(team, kind):

        node = (
            (
                (team.get("last_5") or {})
                .get("goals") or {}
            )
            .get(kind) or {}
        )

        return _num(
            node.get("average")
        )

    # --------------------------------------------------------
    # HOME / AWAY RECORD
    # --------------------------------------------------------

    def venue_record(team, venue):

        fixtures = (
            (team.get("league") or {})
            .get("fixtures") or {}
        )

        node = (
            fixtures.get(venue) or {}
        )

        return {
            "played": node.get("played"),
            "w": node.get("wins"),
            "d": node.get("draws"),
            "l": node.get("loses"),
        }

    # --------------------------------------------------------
    # API-FOOTBALL MODEL
    # --------------------------------------------------------

    percent = (
        item.get("predictions") or {}
    ).get("percent") or {}

    api = {}

    for key in (
        "home",
        "draw",
        "away",
    ):
        value = _num(
            percent.get(key)
        )

        api[key] = (
            value / 100
            if value is not None
            else None
        )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    home_id = home_t.get("id")

    meetings = sorted(
        item.get("h2h") or [],
        key=lambda m: str(
            (m.get("fixture") or {}).get("date")
        ),
        reverse=True,
    )

    rows = []

    for meeting in meetings[:8]:

        goals = meeting.get("goals") or {}

        gh = goals.get("home")
        ga = goals.get("away")

        if gh is None or ga is None:
            continue

        meeting_home_id = (
            (meeting.get("teams") or {})
            .get("home") or {}
        ).get("id")

        home_was_home = (
            meeting_home_id == home_id
        )

        if home_was_home:
            rows.append(
                (gh, ga)
            )
        else:
            rows.append(
                (ga, gh)
            )

    h2h = {
        "n": len(rows),

        "home_w": sum(
            1
            for a, b in rows
            if a > b
        ),

        "draws": sum(
            1
            for a, b in rows
            if a == b
        ),

        "away_w": sum(
            1
            for a, b in rows
            if a < b
        ),

        "avg_goals": (
            sum(a + b for a, b in rows)
            / len(rows)
            if rows
            else None
        ),

        "btts": sum(
            1
            for a, b in rows
            if a > 0 and b > 0
        ),
    }

    # --------------------------------------------------------
    # GOAL AVERAGES
    # --------------------------------------------------------

    gf_h = last5_goal(
        home_t,
        "for",
    )

    ga_h = last5_goal(
        home_t,
        "against",
    )

    gf_a = last5_goal(
        away_t,
        "for",
    )

    ga_a = last5_goal(
        away_t,
        "against",
    )

    # Expected-goal style lambdas.
    #
    # Home attack + away defence
    # Away attack + home defence
    lam_h = (
        (gf_h + ga_a) / 2
        if None not in (gf_h, ga_a)
        else None
    )

    lam_a = (
        (gf_a + ga_h) / 2
        if None not in (gf_a, ga_h)
        else None
    )

    # --------------------------------------------------------
    # BUILD FACTS OBJECT
    # --------------------------------------------------------

    facts = {
        "form_h": form5(home_t),
        "form_a": form5(away_t),

        "gf_h": gf_h,
        "ga_h": ga_h,

        "gf_a": gf_a,
        "ga_a": ga_a,

        "lam_h": lam_h,
        "lam_a": lam_a,

        "rec_h": venue_record(
            home_t,
            "home",
        ),

        "rec_a": venue_record(
            away_t,
            "away",
        ),

        "h2h": h2h,

        "api": api,

        # Keep the raw prediction response available for
        # future upgrades without exposing it to the website.
        "raw": item,
    }

    has_something = any(
        [
            facts["form_h"],
            facts["form_a"],
            facts["h2h"]["n"],
            facts["gf_h"] is not None,
            facts["gf_a"] is not None,
            facts["api"].get("home") is not None,
            facts["api"].get("away") is not None,
        ]
    )

    return facts if has_something else None


# ============================================================
# GET FACTS
# ============================================================

def get_facts(fixture):
    """
    Get football facts for one fixture.

    One /predictions request per fixture.
    Cached for three hours.
    """

    if not fixture:
        return None

    fixture_id = (
        fixture.get("fixture") or {}
    ).get("id")

    if not fixture_id:
        return None

    cached = _facts_cache.get(
        fixture_id
    )

    if (
        cached
        and time.time() - cached[0]
        < FACTS_CACHE_SECONDS
    ):
        return cached[1]

    remaining = getattr(
        bot,
        "_api_remaining",
        None,
    )

    if (
        remaining is not None
        and remaining < MIN_QUOTA
    ):
        _note("quota")
        return None

    try:
        result = bot.football_request(
            "predictions",
            {
                "fixture": fixture_id,
            },
        )

    except Exception as exc:

        print(
            f"Facts for fixture {fixture_id} failed: {exc}"
        )

        _note(
            "facts_error",
            str(exc),
        )

        return None

    response = (
        result.get("response") or []
    )

    facts = (
        parse_facts(response[0])
        if response
        else None
    )

    if facts is None:
        _note("no_data")

    _facts_cache[fixture_id] = (
        time.time(),
        facts,
    )

    return facts


# ============================================================
# FACT SUMMARY
# ============================================================

def facts_digest(facts):
    """
    Produce a short human-readable summary of the football evidence.
    """

    if not facts:
        return (
            "not enough football data"
        )

    h2h = facts["h2h"]

    parts = [
        "last 5 form "
        f"home {facts['form_h'] or '?'} / "
        f"away {facts['form_a'] or '?'}"
    ]

    if (
        facts["gf_h"] is not None
        and facts["ga_h"] is not None
        and facts["gf_a"] is not None
        and facts["ga_a"] is not None
    ):
        parts.append(
            "last-5 goals per game: "
            f"home {facts['gf_h']:.1f} scored / "
            f"{facts['ga_h']:.1f} conceded; "
            f"away {facts['gf_a']:.1f} scored / "
            f"{facts['ga_a']:.1f} conceded"
        )

    if h2h["n"]:

        text = (
            f"last {h2h['n']} meetings: "
            f"home won {h2h['home_w']}, "
            f"draws {h2h['draws']}, "
            f"away won {h2h['away_w']}"
        )

        if h2h["avg_goals"] is not None:
            text += (
                f", {h2h['avg_goals']:.1f} goals/game"
            )

        text += (
            f", BTTS in {h2h['btts']}"
        )

        parts.append(text)

    rec_h = facts["rec_h"]
    rec_a = facts["rec_a"]

    if rec_h.get("played") is not None:
        parts.append(
            f"home record: "
            f"{rec_h.get('w', 0)}W "
            f"{rec_h.get('d', 0)}D "
            f"{rec_h.get('l', 0)}L"
        )

    if rec_a.get("played") is not None:
        parts.append(
            f"away record: "
            f"{rec_a.get('w', 0)}W "
            f"{rec_a.get('d', 0)}D "
            f"{rec_a.get('l', 0)}L"
        )

    api = facts.get("api") or {}

    if (
        api.get("home") is not None
        and api.get("away") is not None
    ):
        parts.append(
            "football model "
            f"home {api['home']:.0%}, "
            f"away {api['away']:.0%}"
        )

    return "; ".join(parts)


# ============================================================
# POISSON MODEL
# ============================================================

def _poisson_probability(k, lam):
    """
    Probability of exactly k goals.
    """

    if lam is None or lam < 0:
        return 0.0

    try:
        return (
            math.exp(-lam)
            * lam ** k
            / math.factorial(k)
        )
    except (OverflowError, ValueError):
        return 0.0


def _poisson_cdf(k, lam):
    """
    Probability of <= k goals.
    """

    if lam is None or lam < 0:
        return 0.0

    total = 0.0

    for i in range(
        int(k) + 1
    ):
        total += _poisson_probability(
            i,
            lam,
        )

    return total


def _poisson_over(line, lam):
    """
    Probability of total goals > line.
    """

    if lam is None:
        return None

    # For lines such as 1.5, 2.5, 3.5:
    # P(over) = 1 - P(total <= floor(line))
    cutoff = math.floor(
        float(line)
    )

    return _clamp(
        1 - _poisson_cdf(
            cutoff,
            lam,
        ),
        0.001,
        0.999,
    )


def _poisson_btts(lam_h, lam_a):
    """
    Probability that both teams score.
    """

    if None in (
        lam_h,
        lam_a,
    ):
        return None

    home_scores = (
        1 - math.exp(-lam_h)
    )

    away_scores = (
        1 - math.exp(-lam_a)
    )

    return _clamp(
        home_scores * away_scores,
        0.001,
        0.999,
    )


def _poisson_win_probs(lam_h, lam_a):
    """
    Estimate home/draw/away probabilities
    from expected goals.
    """

    if None in (
        lam_h,
        lam_a,
    ):
        return None

    home = 0.0
    draw = 0.0
    away = 0.0

    # 0-0 through 10-10 captures practically all
    # probability mass for normal football scores.
    for hg in range(11):
        ph = _poisson_probability(
            hg,
            lam_h,
        )

        for ag in range(11):
            pa = _poisson_probability(
                ag,
                lam_a,
            )

            p = ph * pa

            if hg > ag:
                home += p
            elif hg == ag:
                draw += p
            else:
                away += p

    total = home + draw + away

    if total <= 0:
        return None

    return {
        "home": home / total,
        "draw": draw / total,
        "away": away / total,
    }


# ============================================================
# FORM MODEL
# ============================================================

def _form_strength(form):
    """
    Convert W/D/L form into a simple football strength score.

    W = 1
    D = 0.5
    L = 0
    """

    if not form:
        return None

    chars = [
        c
        for c in form.upper()
        if c in "WDL"
    ]

    if not chars:
        return None

    points = {
        "W": 1.0,
        "D": 0.5,
        "L": 0.0,
    }

    return sum(
        points[c]
        for c in chars
    ) / len(chars)


def _venue_strength(record):
    """
    Convert home/away record into a 0-1 strength score.
    """

    if not record:
        return None

    played = record.get(
        "played"
    )

    if not played:
        return None

    wins = record.get(
        "w"
    ) or 0

    draws = record.get(
        "d"
    ) or 0

    try:
        played = float(played)
        wins = float(wins)
        draws = float(draws)
    except (
        TypeError,
        ValueError,
    ):
        return None

    if played <= 0:
        return None

    return (
        wins + 0.5 * draws
    ) / played


def _h2h_strength(facts):
    """
    H2H strength from the current home team's perspective.
    """

    h2h = facts.get(
        "h2h"
    ) or {}

    n = h2h.get("n") or 0

    if n < 2:
        return None

    home_w = h2h.get(
        "home_w"
    ) or 0

    draws = h2h.get(
        "draws"
    ) or 0

    return (
        home_w + 0.5 * draws
    ) / n


# ============================================================
# EVIDENCE PROBABILITY
# ============================================================

def _evidence_match_strength(facts):
    """
    Build a home-vs-away strength score using football evidence.

    Returns:
        0.0 = strong away evidence
        0.5 = balanced
        1.0 = strong home evidence
    """

    if not facts:
        return 0.5

    components = []
    weights = []

    # --------------------------------------------------------
    # FORM
    # --------------------------------------------------------

    form_h = _form_strength(
        facts.get("form_h")
    )

    form_a = _form_strength(
        facts.get("form_a")
    )

    if (
        form_h is not None
        and form_a is not None
    ):
        total = (
            form_h + form_a
        )

        if total > 0:
            form_home_share = (
                form_h / total
            )
        else:
            form_home_share = 0.5

        components.append(
            form_home_share
        )

        weights.append(
            FORM_WEIGHT
        )

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    gf_h = facts.get("gf_h")
    ga_h = facts.get("ga_h")
    gf_a = facts.get("gf_a")
    ga_a = facts.get("ga_a")

    if None not in (
        gf_h,
        ga_h,
        gf_a,
        ga_a,
    ):

        home_attack = (
            gf_h + ga_a
        ) / 2

        away_attack = (
            gf_a + ga_h
        ) / 2

        total = (
            home_attack
            + away_attack
        )

        if total > 0:
            goal_home_share = (
                home_attack / total
            )
        else:
            goal_home_share = 0.5

        components.append(
            goal_home_share
        )

        weights.append(
            GOALS_WEIGHT
        )

    # --------------------------------------------------------
    # VENUE
    # --------------------------------------------------------

    venue_h = _venue_strength(
        facts.get("rec_h")
    )

    venue_a = _venue_strength(
        facts.get("rec_a")
    )

    if (
        venue_h is not None
        and venue_a is not None
    ):

        total = (
            venue_h + venue_a
        )

        venue_home_share = (
            venue_h / total
            if total > 0
            else 0.5
        )

        components.append(
            venue_home_share
        )

        weights.append(
            VENUE_WEIGHT
        )

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h_strength = _h2h_strength(
        facts
    )

    if h2h_strength is not None:

        components.append(
            h2h_strength
        )

        weights.append(
            H2H_WEIGHT
        )

    # --------------------------------------------------------
    # API-FOOTBALL FOOTBALL MODEL
    # --------------------------------------------------------

    api = facts.get(
        "api"
    ) or {}

    api_h = api.get(
        "home"
    )

    api_a = api.get(
        "away"
    )

    if (
        api_h is not None
        and api_a is not None
    ):

        total = (
            api_h + api_a
        )

        if total > 0:
            api_home_share = (
                api_h / total
            )
        else:
            api_home_share = 0.5

        components.append(
            api_home_share
        )

        weights.append(
            API_MODEL_WEIGHT
        )

    if not components:
        return 0.5

    weighted = sum(
        value * weight
        for value, weight
        in zip(
            components,
            weights,
        )
    )

    weight_total = sum(
        weights
    )

    if weight_total <= 0:
        return 0.5

    return _clamp(
        weighted / weight_total,
        0.08,
        0.92,
    )


def _football_1x2_probability(
    side,
    facts,
):
    """
    Football-only probability for home/away win.

    Combines:
        - Poisson score model
        - recent form
        - venue strength
        - H2H
        - API-Football prediction

    It does NOT use SportyBet odds.
    """

    if not facts:
        return None

    # --------------------------------------------------------
    # POISSON WIN MODEL
    # --------------------------------------------------------

    poisson = _poisson_win_probs(
        facts.get("lam_h"),
        facts.get("lam_a"),
    )

    poisson_p = (
        poisson.get(side)
        if poisson
        else None
    )

    # --------------------------------------------------------
    # FORM/VENUE/H2H STRENGTH
    # --------------------------------------------------------

    strength = _evidence_match_strength(
        facts
    )

    evidence_p = (
        strength
        if side == "home"
        else 1 - strength
    )

    # --------------------------------------------------------
    # API-FOOTBALL MODEL
    # --------------------------------------------------------

    api = facts.get(
        "api"
    ) or {}

    api_p = api.get(
        side
    )

    components = []
    weights = []

    if poisson_p is not None:
        components.append(
            poisson_p
        )
        weights.append(
            0.55
        )

    if evidence_p is not None:
        components.append(
            evidence_p
        )
        weights.append(
            0.25
        )

    if api_p is not None:
        components.append(
            api_p
        )
        weights.append(
            0.20
        )

    if not components:
        return None

    probability = (
        sum(
            p * w
            for p, w
            in zip(
                components,
                weights,
            )
        )
        /
        sum(weights)
    )

    return _clamp(
        probability,
        0.02,
        0.97,
    )


# ============================================================
# MARKET PROBABILITY
# ============================================================

def _football_probability(c, facts):
    """
    Calculate probability for the actual SportyBet market
    using football evidence.

    This is the core function that stops bookmaker odds
    from driving the prediction.
    """

    if not facts:
        return None

    kind = c.get(
        "kind"
    )

    side = c.get(
        "side"
    )

    # --------------------------------------------------------
    # WIN / 1UP / 2UP
    # --------------------------------------------------------

    if kind in (
        "win",
        "up",
        "either_half",
        "dnb",
    ):

        win