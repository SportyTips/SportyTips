"""
SportyTips Football Evidence Engine
-----------------------------------

Architecture:

    SPORTYBET FIXTURE
          ↓
    API-FOOTBALL EVIDENCE
          ↓
    FORM + GOALS + HOME/AWAY + H2H + API MODEL
          ↓
    POISSON GOAL MODEL
          ↓
    MODEL AGREEMENT / DATA QUALITY
          ↓
    FOOTBALL PROBABILITY
          ↓
    WEAK MATCHES REJECTED
          ↓
    SportyBet market/odds checked separately

IMPORTANT:
    SportyBet odds NEVER create the football probability.

This module is designed to remain compatible with the existing
SportyTips / smart_ticket architecture.
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

ENRICH_MAX = int(os.getenv("FOOTBALL_ENRICH_MAX", "10"))
ENRICH_SECONDS = int(os.getenv("FOOTBALL_ENRICH_SECONDS", "45"))

MIN_QUOTA = int(os.getenv("FOOTBALL_MIN_QUOTA", "6"))

FACTS_CACHE_SECONDS = int(
    os.getenv("FOOTBALL_FACTS_CACHE_SECONDS", str(3 * 3600))
)

MIN_ODDS = float(os.getenv("FOOTBALL_MIN_ODDS", "1.30"))

# Probability labels.
ELITE_P = 0.85
VERY_STRONG_P = 0.80
STRONG_P = 0.75
ACCEPTABLE_P = 0.70
WEAK_P = 0.65

# Don't call weak/incomplete data "safe".
REJECT_P = 0.65

# Evidence weights.
FORM_WEIGHT = 0.22
GOALS_WEIGHT = 0.23
VENUE_WEIGHT = 0.15
H2H_WEIGHT = 0.07
API_MODEL_WEIGHT = 0.15
POISSON_WEIGHT = 0.18

# H2H becomes less important when sample is tiny.
MAX_H2H_WEIGHT = H2H_WEIGHT

# Recent form gets slightly more weight than old H2H.
FORM_RESULTS = 5
MAX_H2H = 8

# Don't allow one component to completely dominate.
MAX_COMPONENT_EFFECT = 0.35


# ============================================================
# CACHE / DIAGNOSTICS
# ============================================================

_facts_cache = {}
_fixture_index = {}
_diag = []

_started_at = time.time()
_enriched_count = 0


NO_DATA = (
    "There is not enough football data for this match, "
    "so I will not pretend the model has high confidence."
)


def reset_diag():
    global _diag
    _diag = []


def _note(message):
    if message:
        _diag.append(str(message))


def diag_summary():
    if not _diag:
        return "Football model diagnostics: no warnings."

    unique = []
    seen = set()

    for item in _diag:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    return "Football model diagnostics: " + " | ".join(unique[-12:])


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


# ============================================================
# GENERIC HELPERS
# ============================================================

def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _clamp(value, low=0.0, high=1.0):
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def _safe_average(values, default=None):
    nums = []

    for value in values:
        n = _num(value)

        if n is not None:
            nums.append(n)

    if not nums:
        return default

    return sum(nums) / len(nums)


def _sigmoid(value):
    try:
        return 1.0 / (1.0 + math.exp(-float(value)))
    except Exception:
        return 0.5


def _mean(values, default=0.0):
    nums = [_num(v) for v in values]
    nums = [v for v in nums if v is not None]

    if not nums:
        return default

    return sum(nums) / len(nums)


def _weighted_average(values):
    """
    Values are [(value, weight), ...]
    """
    usable = [
        (float(v), float(w))
        for v, w in values
        if v is not None and w > 0
    ]

    if not usable:
        return None

    total_weight = sum(w for _, w in usable)

    if total_weight <= 0:
        return None

    return sum(v * w for v, w in usable) / total_weight


# ============================================================
# FIXTURE LOOKUP
# ============================================================

def _fixtures_for(date_string):
    now = time.time()

    cached = _fixture_index.get(date_string)

    if cached and now - cached["time"] < 600:
        return cached["fixtures"]

    try:
        fixtures = bot.get_allowed_fixtures_cached(date_string) or []

        _fixture_index[date_string] = {
            "time": now,
            "fixtures": fixtures,
        }

        return fixtures

    except Exception as exc:
        _note(f"Fixture lookup failed: {exc}")
        return []


def find_fixture(event):
    """
    Match a SportyBet event to the corresponding API-Football fixture.
    """

    if not event:
        return None

    kickoff = (
        event.get("kickoff")
        or event.get("startTime")
        or event.get("start_time")
    )

    if kickoff is None:
        return None

    try:
        if isinstance(kickoff, (int, float)):
            dt = datetime.fromtimestamp(
                kickoff / 1000 if kickoff > 10_000_000_000 else kickoff,
                tz=timezone.utc,
            )
        else:
            text = str(kickoff).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

    except Exception:
        return None

    date_string = dt.astimezone(bot.LOCAL_TZ).strftime("%Y-%m-%d")

    fixtures = _fixtures_for(date_string)

    home_name = _clean(
        event.get("home")
        or event.get("homeTeam")
        or event.get("home_name")
    )

    away_name = _clean(
        event.get("away")
        or event.get("awayTeam")
        or event.get("away_name")
    )

    best = None
    best_score = 0.0

    for fixture in fixtures:
        try:
            fx = fixture.get("fixture", {})
            teams = fixture.get("teams", {})

            fixture_time = fx.get("date")

            if not fixture_time:
                continue

            fdt = datetime.fromisoformat(
                str(fixture_time).replace("Z", "+00:00")
            )

            if fdt.tzinfo is None:
                fdt = fdt.replace(tzinfo=timezone.utc)

            diff_minutes = abs(
                (fdt.astimezone(timezone.utc) - dt.astimezone(timezone.utc))
                .total_seconds()
            ) / 60

            if diff_minutes > 45:
                continue

            fh = _clean(teams.get("home", {}).get("name"))
            fa = _clean(teams.get("away", {}).get("name"))

            try:
                home_sim = sp._sim(home_name, fh)
                away_sim = sp._sim(away_name, fa)
            except Exception:
                home_sim = 1.0 if home_name.lower() == fh.lower() else 0.0
                away_sim = 1.0 if away_name.lower() == fa.lower() else 0.0

            score = (home_sim + away_sim) / 2

            if score > best_score:
                best_score = score
                best = fixture

        except Exception:
            continue

    if best is None or best_score < 0.72:
        return None

    return best


# ============================================================
# API REQUEST
# ============================================================

def _api_remaining():
    try:
        return int(getattr(bot, "_api_remaining", 100))
    except Exception:
        return 100


# ============================================================
# FORM PARSING
# ============================================================

def _parse_form_string(form):
    """
    Convert something like:

        WWDLW

    into numerical strength.

    Win = 1
    Draw = 0.5
    Loss = 0
    """

    if not form:
        return None

    text = str(form).upper()

    values = []

    for char in text:
        if char == "W":
            values.append(1.0)
        elif char == "D":
            values.append(0.5)
        elif char == "L":
            values.append(0.0)

    if not values:
        return None

    return sum(values) / len(values)


def _recent_goal_strength(gf, ga):
    """
    Converts recent scoring/conceding into a bounded strength value.

    More goals scored and fewer conceded = stronger.
    """

    gf = _num(gf)
    ga = _num(ga)

    if gf is None or ga is None:
        return None

    # Typical football scoring difference is usually modest.
    diff = gf - ga

    return _clamp(
        0.5 + (diff / 4.0),
        0.05,
        0.95,
    )


# ============================================================
# FACT EXTRACTION
# ============================================================

def parse_facts(item):
    """
    Parse API-Football /predictions response.

    Returns a compact football evidence object.
    """

    if not item:
        return None

    fixture = item.get("fixture", {})
    teams = item.get("teams", {})
    home = item.get("home", {}) or {}
    away = item.get("away", {}) or {}
    predictions = item.get("predictions", {}) or {}

    home_team = (
        teams.get("home", {})
        if isinstance(teams.get("home"), dict)
        else {}
    )

    away_team = (
        teams.get("away", {})
        if isinstance(teams.get("away"), dict)
        else {}
    )

    home_name = (
        home_team.get("name")
        or home.get("name")
        or "Home"
    )

    away_name = (
        away_team.get("name")
        or away.get("name")
        or "Away"
    )

    home_id = home_team.get("id") or home.get("id")
    away_id = away_team.get("id") or away.get("id")

    # --------------------------------------------------------
    # FORM
    # --------------------------------------------------------

    home_form_raw = (
        home.get("last_5", {}).get("form")
        or home.get("league", {}).get("form")
        or home.get("form")
    )

    away_form_raw = (
        away.get("last_5", {}).get("form")
        or away.get("league", {}).get("form")
        or away.get("form")
    )

    home_form = _parse_form_string(home_form_raw)
    away_form = _parse_form_string(away_form_raw)

    # --------------------------------------------------------
    # LAST 5 GOALS
    # --------------------------------------------------------

    home_last5 = home.get("last_5", {}) or {}
    away_last5 = away.get("last_5", {}) or {}

    home_goals = home_last5.get("goals", {}) or {}
    away_goals = away_last5.get("goals", {}) or {}

    home_gf = _num(
        home_goals.get("for", {}).get("total")
        if isinstance(home_goals.get("for"), dict)
        else home_goals.get("for")
    )

    home_ga = _num(
        home_goals.get("against", {}).get("total")
        if isinstance(home_goals.get("against"), dict)
        else home_goals.get("against")
    )

    away_gf = _num(
        away_goals.get("for", {}).get("total")
        if isinstance(away_goals.get("for"), dict)
        else away_goals.get("for")
    )

    away_ga = _num(
        away_goals.get("against", {}).get("total")
        if isinstance(away_goals.get("against"), dict)
        else away_goals.get("against")
    )

    # API sometimes provides totals rather than averages.
    # Convert when possible.
    if home_gf is not None and home_gf > 5:
        home_gf /= FORM_RESULTS

    if home_ga is not None and home_ga > 5:
        home_ga /= FORM_RESULTS

    if away_gf is not None and away_gf > 5:
        away_gf /= FORM_RESULTS

    if away_ga is not None and away_ga > 5:
        away_ga /= FORM_RESULTS

    # --------------------------------------------------------
    # VENUE RECORD
    # --------------------------------------------------------

    home_league = home.get("league", {}) or {}
    away_league = away.get("league", {}) or {}

    home_fixtures = home_league.get("fixtures", {}) or {}
    away_fixtures = away_league.get("fixtures", {}) or {}

    home_home_record = home_fixtures.get("home", {}) or {}
    away_away_record = away_fixtures.get("away", {}) or {}

    # API-Football commonly exposes:
    #
    # wins.total
    # draws.total
    # loses.total

    def record_points(record):
        if not isinstance(record, dict):
            return None

        wins = _num(
            record.get("wins", {}).get("total")
            if isinstance(record.get("wins"), dict)
            else record.get("wins")
        )

        draws = _num(
            record.get("draws", {}).get("total")
            if isinstance(record.get("draws"), dict)
            else record.get("draws")
        )

        losses = _num(
            record.get("loses", {}).get("total")
            if isinstance(record.get("loses"), dict)
            else record.get("losses")
        )

        if wins is None and draws is None and losses is None:
            return None

        wins = wins or 0
        draws = draws or 0
        losses = losses or 0

        total = wins + draws + losses

        if total <= 0:
            return None

        return {
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "points_rate": (
                wins * 3 + draws
            ) / (total * 3),
            "sample": total,
        }

    home_venue = record_points(home_home_record)
    away_venue = record_points(away_away_record)

    # --------------------------------------------------------
    # API-FOOTBALL MODEL
    # --------------------------------------------------------

    percent = predictions.get("percent", {}) or {}

    api_home = _num(
        percent.get("home"),
        None,
    )

    api_draw = _num(
        percent.get("draw"),
        None,
    )

    api_away = _num(
        percent.get("away"),
        None,
    )

    if api_home is not None:
        api_home /= 100.0

    if api_draw is not None:
        api_draw /= 100.0

    if api_away is not None:
        api_away /= 100.0

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = predictions.get("h2h") or []

    if not isinstance(h2h, list):
        h2h = []

    h2h = h2h[:MAX_H2H]

    h2h_home_wins = 0
    h2h_draws = 0
    h2h_away_wins = 0
    h2h_total_goals = []
    h2h_btts = 0

    for match in h2h:
        try:
            teams2 = match.get("teams", {}) or {}
            goals2 = match.get("goals", {}) or {}

            hteam = teams2.get("home", {}) or {}
            ateam = teams2.get("away", {}) or {}

            hg = _num(goals2.get("home"))
            ag = _num(goals2.get("away"))

            if hg is None or ag is None:
                continue

            h2h_total_goals.append(hg + ag)

            if hg > 0 and ag > 0:
                h2h_btts += 1

            hteam_id = hteam.get("id")
            ateam_id = ateam.get("id")

            if hteam_id == home_id and ateam_id == away_id:
                if hg > ag:
                    h2h_home_wins += 1
                elif hg == ag:
                    h2h_draws += 1
                else:
                    h2h_away_wins += 1

            elif hteam_id == away_id and ateam_id == home_id:
                if hg > ag:
                    h2h_away_wins += 1
                elif hg == ag:
                    h2h_draws += 1
                else:
                    h2h_home_wins += 1

        except Exception:
            continue

    h2h_sample = (
        h2h_home_wins +
        h2h_draws +
        h2h_away_wins
    )

    h2h_home_rate = None
    h2h_away_rate = None

    if h2h_sample:
        h2h_home_rate = (
            h2h_home_wins + 0.5 * h2h_draws
        ) / h2h_sample

        h2h_away_rate = (
            h2h_away_wins + 0.5 * h2h_draws
        ) / h2h_sample

    h2h_avg_goals = _safe_average(
        h2h_total_goals,
        None,
    )

    h2h_btts_rate = (
        h2h_btts / h2h_sample
        if h2h_sample
        else None
    )

    # --------------------------------------------------------
    # EXPECTED GOALS / LAMBDAS
    # --------------------------------------------------------

    # Baseline recent attacking/defensive model.
    #
    # Home expected goals:
    #   home scoring + away conceding
    #
    # Away expected goals:
    #   away scoring + home conceding

    lambda_home = None
    lambda_away = None

    if (
        home_gf is not None
        and away_ga is not None
    ):
        lambda_home = (
            home_gf + away_ga
        ) / 2.0

    if (
        away_gf is not None
        and home_ga is not None
    ):
        lambda_away = (
            away_gf + home_ga
        ) / 2.0

    # Keep extreme estimates under control.
    if lambda_home is not None:
        lambda_home = _clamp(
            lambda_home,
            0.15,
            4.50,
        )

    if lambda_away is not None:
        lambda_away = _clamp(
            lambda_away,
            0.15,
            4.50,
        )

    # --------------------------------------------------------
    # DATA QUALITY
    # --------------------------------------------------------

    available = 0
    possible = 0

    checks = [
        home_form is not None and away_form is not None,
        home_gf is not None and home_ga is not None,
        away_gf is not None and away_ga is not None,
        home_venue is not None,
        away_venue is not None,
        api_home is not None and api_away is not None,
        lambda_home is not None and lambda_away is not None,
        h2h_sample >= 2,
    ]

    for check in checks:
        possible += 1

        if check:
            available += 1

    data_quality = (
        available / possible
        if possible
        else 0.0
    )

    return {
        "fixture_id": fixture.get("id"),

        "home": home_name,
        "away": away_name,

        "home_id": home_id,
        "away_id": away_id,

        "home_form_raw": home_form_raw,
        "away_form_raw": away_form_raw,

        "home_form": home_form,
        "away_form": away_form,

        "home_gf": home_gf,
        "home_ga": home_ga,

        "away_gf": away_gf,
        "away_ga": away_ga,

        "home_venue": home_venue,
        "away_venue": away_venue,

        "api_home": api_home,
        "api_draw": api_draw,
        "api_away": api_away,

        "h2h_sample": h2h_sample,
        "h2h_home_wins": h2h_home_wins,
        "h2h_draws": h2h_draws,
        "h2h_away_wins": h2h_away_wins,
        "h2h_home_rate": h2h_home_rate,
        "h2h_away_rate": h2h_away_rate,
        "h2h_avg_goals": h2h_avg_goals,
        "h2h_btts_rate": h2h_btts_rate,

        "lambda_home": lambda_home,
        "lambda_away": lambda_away,

        "data_quality": data_quality,

        "raw": item,
    }


# ============================================================
# GET FOOTBALL FACTS
# ============================================================

def get_facts(fixture):
    global _enriched_count

    if not fixture:
        return None

    fixture_id = (
        fixture.get("fixture", {}).get("id")
        or fixture.get("id")
    )

    if not fixture_id:
        return None

    now = time.time()

    cached = _facts_cache.get(fixture_id)

    if cached:
        age = now - cached["time"]

        if age < FACTS_CACHE_SECONDS:
            return cached["facts"]

    if _api_remaining() < MIN_QUOTA:
        _note(
            "API-Football quota is low; "
            "skipping expensive football enrichment."
        )
        return None

    if _enriched_count >= ENRICH_MAX:
        _note(
            f"Football enrichment limit reached ({ENRICH_MAX})."
        )
        return None

    if time.time() - _started_at > ENRICH_SECONDS:
        _note("Football enrichment time limit reached.")
        return None

    try:
        result = bot.football_request(
            "/predictions",
            {
                "fixture": fixture_id,
            },
        )

        _enriched_count += 1

        response = result.get("response", [])

        if not response:
            _note(
                f"No API-Football prediction data for fixture {fixture_id}."
            )
            return None

        facts = parse_facts(response[0])

        if not facts:
            return None

        _facts_cache[fixture_id] = {
            "time": now,
            "facts": facts,
        }

        return facts

    except Exception as exc:
        _note(
            f"Football data request failed for {fixture_id}: {exc}"
        )
        return None


# ============================================================
# POISSON MODEL
# ============================================================

def _poisson_probability(lam, goals):
    if lam is None:
        return 0.0

    try:
        return (
            math.exp(-lam)
            * (lam ** goals)
            / math.factorial(goals)
        )
    except Exception:
        return 0.0


def _score_matrix(lambda_home, lambda_away, max_goals=8):
    matrix = []

    if lambda_home is None or lambda_away is None:
        return matrix

    for home_goals in range(max_goals + 1):
        row = []

        ph = _poisson_probability(
            lambda_home,
            home_goals,
        )

        for away_goals in range(max_goals + 1):
            pa = _poisson_probability(
                lambda_away,
                away_goals,
            )

            row.append(ph * pa)

        matrix.append(row)

    return matrix


def _poisson_win_probs(
    lambda_home,
    lambda_away,
):
    matrix = _score_matrix(
        lambda_home,
        lambda_away,
    )

    if not matrix:
        return None

    home = 0.0
    draw = 0.0
    away = 0.0

    for hg, row in enumerate(matrix):
        for ag, probability in enumerate(row):

            if hg > ag:
                home += probability

            elif hg == ag:
                draw += probability

            else:
                away += probability

    total = home + draw + away

    if total <= 0:
        return None

    return {
        "home": home / total,
        "draw": draw / total,
        "away": away / total,
    }


def _poisson_over(
    lambda_home,
    lambda_away,
    line,
):
    if lambda_home is None or lambda_away is None:
        return None

    total_lambda = lambda_home + lambda_away

    if total_lambda <= 0:
        return None

    # Probability total goals > line.
    whole = int(math.floor(line))

    under = 0.0

    for goals in range(whole + 1):
        under += _poisson_probability(
            total_lambda,
            goals,
        )

    if line == whole:
        return _clamp(1.0 - under)

    # Approximation for non-integer lines.
    return _clamp(1.0 - under)


def _poisson_btts(
    lambda_home,
    lambda_away,
):
    if lambda_home is None or lambda_away is None:
        return None

    home_zero = math.exp(-lambda_home)
    away_zero = math.exp(-lambda_away)

    both_zero = (
        home_zero * away_zero
    )

    return _clamp(
        1.0
        - home_zero
        - away_zero
        + both_zero
    )


def _poisson_team_goals(
    lam,
    line,
):
    if lam is None:
        return None

    whole = int(math.floor(line))

    probability = 0.0

    for goals in range(whole + 1):
        probability += _poisson_probability(
            lam,
            goals,
        )

    return _clamp(1.0 - probability)


# ============================================================
# FOOTBALL EVIDENCE COMPONENTS
# ============================================================

def _form_strength(facts):
    if not facts:
        return None

    h = facts.get("home_form")
    a = facts.get("away_form")

    if h is None or a is None:
        return None

    total = h + a

    if total <= 0:
        return 0.5

    return _clamp(
        h / total,
        0.05,
        0.95,
    )


def _goal_strength(facts):
    if not facts:
        return None

    h_gf = facts.get("home_gf")
    h_ga = facts.get("home_ga")

    a_gf = facts.get("away_gf")
    a_ga = facts.get("away_ga")

    home = _recent_goal_strength(
        h_gf,
        h_ga,
    )

    away = _recent_goal_strength(
        a_gf,
        a_ga,
    )

    if home is None or away is None:
        return None

    # Convert relative strengths into a home probability.
    total = home + away

    if total <= 0:
        return 0.5

    return _clamp(
        home / total,
        0.05,
        0.95,
    )


def _venue_strength(facts):
    if not facts:
        return None

    h = facts.get("home_venue")
    a = facts.get("away_venue")

    if not h or not a:
        return None

    hp = _num(h.get("points_rate"))
    ap = _num(a.get("points_rate"))

    if hp is None or ap is None:
        return None

    total = hp + ap

    if total <= 0:
        return 0.5

    return _clamp(
        hp / total,
        0.05,
        0.95,
    )


def _h2h_strength(facts):
    if not facts:
        return None

    sample = facts.get("h2h_sample", 0)

    if sample < 2:
        return None

    h = facts.get("h2h_home_rate")
    a = facts.get("h2h_away_rate")

    if h is None or a is None:
        return None

    total = h + a

    if total <= 0:
        return 0.5

    raw = h / total

    # Small H2H sample gets shrunk strongly toward 50/50.
    reliability = _clamp(
        sample / MAX_H2H,
        0.15,
        1.0,
    )

    return _clamp(
        0.5
        + (raw - 0.5) * reliability,
        0.05,
        0.95,
    )


def _api_model_strength(facts):
    if not facts:
        return None

    h = facts.get("api_home")
    a = facts.get("api_away")

    if h is None or a is None:
        return None

    total = h + a

    if total <= 0:
        return None

    return _clamp(
        h / total,
        0.05,
        0.95,
    )


# ============================================================
# MODEL AGREEMENT
# ============================================================

def _agreement(values):
    """
    Measures how closely the different football models agree.

    1.0 = excellent agreement
    0.0 = major disagreement
    """

    usable = [
        float(v)
        for v in values
        if v is not None
    ]

    if len(usable) < 2:
        return 0.55

    spread = max(usable) - min(usable)

    # 0 spread -> 1.0
    # 0.20 spread -> ~0.50
    # 0.40+ spread -> very poor
    score = 1.0 - (spread / 0.40)

    return _clamp(
        score,
        0.20,
        1.0,
    )


# ============================================================
# MAIN 1X2 FOOTBALL MODEL
# ============================================================

def _football_1x2_model(facts):
    """
    Produces football-only probabilities.

    NO SPORTYBET ODDS ARE USED HERE.
    """

    if not facts:
        return None

    form = _form_strength(facts)
    goals = _goal_strength(facts)
    venue = _venue_strength(facts)
    h2h = _h2h_strength(facts)
    api_model = _api_model_strength(facts)

    poisson = _poisson_win_probs(
        facts.get("lambda_home"),
        facts.get("lambda_away"),
    )

    poisson_home = (
        poisson.get("home")
        if poisson
        else None
    )

    components = [
        form,
        goals,
        venue,
        h2h,
        api_model,
        poisson_home,
    ]

    # Calculate the evidence probability.
    weighted = _weighted_average([
        (form, FORM_WEIGHT),
        (goals, GOALS_WEIGHT),
        (venue, VENUE_WEIGHT),
        (h2h, H2H_WEIGHT),
        (api_model, API_MODEL_WEIGHT),
        (poisson_home, POISSON_WEIGHT),
    ])

    if weighted is None:
        return None

    agreement = _agreement(components)

    quality = _clamp(
        facts.get("data_quality", 0.0)
    )

    # Data quality and model agreement are not optional.
    #
    # If the models disagree, confidence is reduced.
    #
    # This prevents:
    #
    # model A = 88%
    # model B = 52%
    # model C = 81%
    #
    # from being blindly called "88% sure".
    confidence_factor = (
        0.65
        + 0.20 * quality
        + 0.15 * agreement
    )

    home_probability = _clamp(
        0.5
        + (weighted - 0.5)
        * confidence_factor
    )

    # Draw gets its own Poisson probability.
    draw_probability = (
        poisson.get("draw")
        if poisson
        else None
    )

    if draw_probability is None:
        draw_probability = 0.25

    # Away is the remaining side.
    away_probability = _clamp(
        1.0
        - home_probability
        - draw_probability
    )

    # Normalize.
    total = (
        home_probability
        + draw_probability
        + away_probability
    )

    if total <= 0:
        return None

    home_probability /= total
    draw_probability /= total
    away_probability /= total

    return {
        "home": _clamp(home_probability),
        "draw": _clamp(draw_probability),
        "away": _clamp(away_probability),

        "form": form,
        "goals": goals,
        "venue": venue,
        "h2h": h2h,
        "api_model": api_model,
        "poisson": poisson_home,

        "agreement": agreement,
        "quality": quality,
    }


# ============================================================
# 1UP / 2UP
# ============================================================

def _lead_probability(
    lambda_home,
    lambda_away,
    side,
    required_lead,
):
    """
    Conservative approximation for reaching a positive goal lead.

    IMPORTANT:
        This is NOT simply:

            win probability + 12%

    The old system did that, which artificially inflated 1UP/2UP.

    We instead use the final-score distribution as a conservative
    proxy and apply a penalty because final-score distribution does
    not know the exact minute-by-minute scoring path.
    """

    matrix = _score_matrix(
        lambda_home,
        lambda_away,
    )

    if not matrix:
        return None

    probability = 0.0

    for hg, row in enumerate(matrix):
        for ag, p in enumerate(row):

            if side == "home":
                margin = hg - ag
            else:
                margin = ag - hg

            if margin >= required_lead:
                probability += p

    # Final-margin probability is not exactly the same as
    # "team reached a lead at any time".
    #
    # Keep it conservative instead of inventing a large uplift.
    probability *= 0.92

    return _clamp(probability)


# ============================================================
# HANDICAPS
# ============================================================

def _handicap_probability(
    facts,
    side,
    line,
):
    """
    Probability of covering a simple positive handicap.

    Supports common whole / half lines.

    Quarter-line Asian handicaps are treated conservatively.
    """

    if not facts:
        return None

    matrix = _score_matrix(
        facts.get("lambda_home"),
        facts.get("lambda_away"),
    )

    if not matrix:
        return None

    line = _num(line)

    if line is None:
        return None

    probability = 0.0

    for hg, row in enumerate(matrix):
        for ag, p in enumerate(row):

            margin = (
                hg - ag
                if side == "home"
                else ag - hg
            )

            adjusted = margin + line

            if adjusted > 0:
                probability += p

            elif adjusted == 0:
                # Push does not lose.
                probability += p * 0.50

    return _clamp(probability)


# ============================================================
# MARKET PROBABILITY
# ============================================================

def _football_probability(candidate, facts):
    """
    Calculate probability using football evidence only.

    Candidate shape may vary slightly depending on smart_ticket,
    so this function intentionally accepts multiple common field names.
    """

    if not facts or not candidate:
        return None

    kind = str(
        candidate.get("kind")
        or candidate.get("market_kind")
        or ""
    ).lower()

    side = str(
        candidate.get("side")
        or candidate.get("selection")
        or candidate.get("team_side")
        or ""
    ).lower()

    line = candidate.get("line")

    model = _football_1x2_model(facts)

    if not model:
        return None

    # --------------------------------------------------------
    # WIN
    # --------------------------------------------------------

    if kind in {
        "win",
        "1x2",
        "straight_win",
        "up",
        "1up",
        "2up",
    }:

        if side in {"home", "h", "1"}:
            win_p = model["home"]
        elif side in {"away", "a", "2"}:
            win_p = model["away"]
        else:
            return None

        if kind in {"1up", "2up"}:
            required_lead = (
                1
                if kind == "1up"
                else 2
            )

            lead_p = _lead_probability(
                facts.get("lambda_home"),
                facts.get("lambda_away"),
                "home"
                if side in {"home", "h", "1"}
                else "away",
                required_lead,
            )

            if lead_p is not None:
                return lead_p

        return win_p

    # --------------------------------------------------------
    # DOUBLE CHANCE
    # --------------------------------------------------------

    if kind in {
        "dc",
        "double_chance",
    }:

        if side in {"12", "homeaway", "home_away"}:
            return _clamp(
                model["home"]
                + model["away"]
            )

        if side in {"1x", "homedraw"}:
            return _clamp(
                model["home"]
                + model["draw"]
            )

        if side in {"x2", "drawaway"}:
            return _clamp(
                model["draw"]
                + model["away"]
            )

        return None

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    if kind in {
        "over",
        "over15",
        "goals",
        "total",
    }:

        if line is None:
            line = 1.5

        return _poisson_over(
            facts.get("lambda_home"),
            facts.get("lambda_away"),
            float(line),
        )

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    if kind in {
        "btts",
        "btts_yes",
    }:

        return _poisson_btts(
            facts.get("lambda_home"),
            facts.get("lambda_away"),
        )

    # --------------------------------------------------------
    # TEAM GOALS
    # --------------------------------------------------------

    if kind in {
        "team_goals",
        "home_team_goals",
        "away_team_goals",
    }:

        if line is None:
            line = 0.5

        if kind == "away_team_goals":
            lam = facts.get("lambda_away")
        elif kind == "home_team_goals":
            lam = facts.get("lambda_home")
        elif side in {"away", "a", "2"}:
            lam = facts.get("lambda_away")
        else:
            lam = facts.get("lambda_home")

        return _poisson_team_goals(
            lam,
            float(line),
        )

    # --------------------------------------------------------
    # POSITIVE HANDICAP
    # --------------------------------------------------------

    if kind in {
        "handicap",
        "asian_handicap",
        "positive_handicap",
        "positive_asian_handicap",
    }:

        if line is None:
            return None

        # We only want positive handicaps.
        if float(line) <= 0:
            return None

        if side in {"home", "h", "1"}:
            handicap_side = "home"
        elif side in {"away", "a", "2"}:
            handicap_side = "away"
        else:
            return None

        return _handicap_probability(
            facts,
            handicap_side,
            float(line),
        )

    # --------------------------------------------------------
    # CORNERS
    # --------------------------------------------------------

    if kind in {
        "corners",
        "corners_1h",
        "corner",
    }:
        # We do NOT have reliable corner evidence yet.
        #
        # Returning None is intentional.
        #
        # Never pretend that a football model knows corners
        # when the required corner dataset was never fetched.
        return None

    # --------------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------------

    return None


# ============================================================
# PUBLIC PROBABILITY FUNCTION
# ============================================================

def adjusted_p(candidate, facts):
    """
    Public compatibility function used by smart_ticket.

    IMPORTANT:
        This is football probability.

        It does NOT read SportyBet odds.
    """

    if not facts:
        return None

    probability = _football_probability(
        candidate,
        facts,
    )

    if probability is None:
        return None

    model = _football_1x2_model(facts)

    if not model:
        return None

    quality = model.get(
        "quality",
        0.0,
    )

    agreement = model.get(
        "agreement",
        0.0,
    )

    # Evidence penalty.
    #
    # Complete + agreeing data:
    #       almost no penalty
    #
    # Weak/incomplete data:
    #       noticeable penalty
    #
    # This prevents weak matches from reaching the top.
    penalty = (
        0.90
        + 0.07 * quality
        + 0.03 * agreement
    )

    probability *= penalty

    return _clamp(
        probability,
        0.01,
        0.97,
    )


# ============================================================
# CONFIDENCE LABEL
# ============================================================

def confidence_label(probability):
    if probability is None:
        return "REJECT"

    probability = float(probability)

    if probability >= ELITE_P:
        return "ELITE"

    if probability >= VERY_STRONG_P:
        return "VERY STRONG"

    if probability >= STRONG_P:
        return "STRONG"

    if probability >= ACCEPTABLE_P:
        return "ACCEPTABLE"

    if probability >= WEAK_P:
        return "WEAK"

    return "REJECT"


# ============================================================
# REASONS
# ============================================================

def _cap(value, digits=2):
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _win_like(candidate):
    kind = str(
        candidate.get("kind")
        or ""
    ).lower()

    return kind in {
        "win",
        "1x2",
        "up",
        "1up",
        "2up",
        "straight_win",
    }


def _goals_like(candidate):
    kind = str(
        candidate.get("kind")
        or ""
    ).lower()

    return kind in {
        "goals",
        "over",
        "over15",
        "btts",
        "btts_yes",
        "team_goals",
        "home_team_goals",
        "away_team_goals",
    }


def _dc_like(candidate):
    kind = str(
        candidate.get("kind")
        or ""
    ).lower()

    return kind in {
        "dc",
        "double_chance",
    }


def reason_for(candidate, facts, probability=None):
    """
    Human-readable explanation for the football model.

    No odds-based reasoning.
    """

    if not facts:
        return NO_DATA

    if probability is None:
        probability = adjusted_p(
            candidate,
            facts,
        )

    label = confidence_label(probability)

    home = facts.get("home", "Home")
    away = facts.get("away", "Away")

    home_form = facts.get("home_form")
    away_form = facts.get("away_form")

    home_gf = facts.get("home_gf")
    home_ga = facts.get("home_ga")

    away_gf = facts.get("away_gf")
    away_ga = facts.get("away_ga")

    reasons = []

    if home_form is not None and away_form is not None:
        if home_form > away_form + 0.10:
            reasons.append(
                f"{home} has the stronger recent form"
            )
        elif away_form > home_form + 0.10:
            reasons.append(
                f"{away} has the stronger recent form"
            )

    if (
        home_gf is not None
        and home_ga is not None
        and away_gf is not None
        and away_ga is not None
    ):
        if home_gf > away_gf + 0.25:
            reasons.append(
                f"{home} has been more productive in attack"
            )

        if away_gf > home_gf + 0.25:
            reasons.append(
                f"{away} has been more productive in attack"
            )

        if home_ga < away_ga - 0.25:
            reasons.append(
                f"{home} has the better recent defensive numbers"
            )

        if away_ga < home_ga - 0.25:
            reasons.append(
                f"{away} has the better recent defensive numbers"
            )

    venue = facts.get("home_venue")
    away_venue = facts.get("away_venue")

    if venue and away_venue:
        hp = venue.get("points_rate")
        ap = away_venue.get("points_rate")

        if hp is not None and ap is not None:
            if hp > ap + 0.15:
                reasons.append(
                    f"{home} has the stronger home record"
                )

            elif ap > hp + 0.15:
                reasons.append(
                    f"{away} has the stronger away record"
                )

    h2h_sample = facts.get("h2h_sample", 0)

    if h2h_sample >= 3:
        h2h_home = facts.get("h2h_home_rate")
        h2h_away = facts.get("h2h_away_rate")

        if (
            h2h_home is not None
            and h2h_away is not None
        ):
            if h2h_home > h2h_away + 0.15:
                reasons.append(
                    f"H2H has leaned toward {home}"
                )

            elif h2h_away > h2h_home + 0.15:
                reasons.append(
                    f"H2H has leaned toward {away}"
                )

    lam_h = facts.get("lambda_home")
    lam_a = facts.get("lambda_away")

    if lam_h is not None and lam_a is not None:
        if lam_h + lam_a >= 2.4:
            reasons.append(
                "the goal model expects an open match"
            )

        elif lam_h + lam_a <= 2.0:
            reasons.append(
                "the goal model expects a lower-scoring match"
            )

    if not reasons:
        reasons.append(
            "the available football evidence is relatively balanced"
        )

    quality = facts.get(
        "data_quality",
        0.0,
    )

    agreement = (
        _football_1x2_model(facts) or {}
    ).get(
        "agreement",
        0.0,
    )

    reasons.append(
        f"model confidence {label.lower()} "
        f"({probability * 100:.1f}%)"
    )

    reasons.append(
        f"data quality {quality * 100:.0f}%"
    )

    if agreement < 0.55:
        reasons.append(
            "models disagree, so confidence is reduced"
        )

    return "; ".join(reasons) + "."


# ============================================================
# FACTS DIGEST
# ============================================================

def facts_digest(facts):
    if not facts:
        return NO_DATA

    home = facts.get("home", "Home")
    away = facts.get("away", "Away")

    parts = []

    hf = facts.get("home_form_raw")
    af = facts.get("away_form_raw")

    if hf or af:
        parts.append(
            f"Form: {home} {hf or '?'} / {away} {af or '?'}"
        )

    hgf = facts.get("home_gf")
    hga = facts.get("home_ga")
    agf = facts.get("away_gf")
    aga = facts.get("away_ga")

    if (
        hgf is not None
        and hga is not None
        and agf is not None
        and aga is not None
    ):
        parts.append(
            f"Recent goals: {home} "
            f"{hgf:.2f} scored / {hga:.2f} conceded; "
            f"{away} {agf:.2f} scored / {aga:.2f} conceded"
        )

    venue = facts.get("home_venue")
    away_venue = facts.get("away_venue")

    if venue:
        parts.append(
            f"{home} home record: "
            f"{venue['wins']:.0f}W "
            f"{venue['draws']:.0f}D "
            f"{venue['losses']:.0f}L"
        )

    if away_venue:
        parts.append(
            f"{away} away record: "
            f"{away_venue['wins']:.0f}W "
            f"{away_venue['draws']:.0f}D "
            f"{away_venue['losses']:.0f}L"
        )

    h2h_sample = facts.get("h2h_sample", 0)

    if h2h_sample:
        parts.append(
            f"H2H sample: {h2h_sample}"
        )

    lam_h = facts.get("lambda_home")
    lam_a = facts.get("lambda_away")

    if lam_h is not None and lam_a is not None:
        parts.append(
            f"Expected goals model: "
            f"{home} {lam_h:.2f}, "
            f"{away} {lam_a:.2f}"
        )

    model = _football_1x2_model(facts)

    if model:
        parts.append(
            "Football model: "
            f"{home} {model['home'] * 100:.1f}%, "
            f"Draw {model['draw'] * 100:.1f}%, "
            f"{away} {model['away'] * 100:.1f}%"
        )

        parts.append(
            f"Data quality: "
            f"{model['quality'] * 100:.0f}%"
        )

        parts.append(
            f"Model agreement: "
            f"{model['agreement'] * 100:.0f}%"
        )

    return " | ".join(parts)


# ============================================================
# EVENT HELPERS
# ============================================================

def event_for(event):
    """
    Compatibility helper.
    """

    fixture = find_fixture(event)

    if not fixture:
        return None

    return get_facts(fixture)


def classify_leg(candidate):
    """
    Compatibility helper used by existing code.
    """

    if not candidate:
        return "unknown"

    kind = str(
        candidate.get("kind")
        or ""
    ).lower()

    if kind in {
        "1up",
        "2up",
        "up",
        "win",
        "straight_win",
    }:
        return "up"

    if kind in {
        "over",
        "over15",
        "goals",
        "team_goals",
        "btts",
        "btts_yes",
    }:
        return "goals"

    if kind in {
        "dc",
        "double_chance",
    }:
        return "dc"

    if kind in {
        "corners",
        "corners_1h",
    }:
        return "corners"

    if kind in {
        "handicap",
        "asian_handicap",
        "positive_handicap",
        "positive_asian_handicap",
    }:
        return "handicap"

    return kind or "unknown"


def why(event, candidate):
    facts = event_for(event)

    if not facts:
        return NO_DATA

    p = adjusted_p(
        candidate,
        facts,
    )

    return reason_for(
        candidate,
        facts,
        p,
    )


# ============================================================
# RANKING
# ============================================================

def _candidate_odds(candidate):
    return _num(
        candidate.get("odds")
        or candidate.get("odd")
        or candidate.get("price")
    )


def _candidate_probability(candidate, facts):
    return adjusted_p(
        candidate,
        facts,
    )


def _rank(provider, event, use_data=True):
    """
    Rank SportyBet markets using football probability.

    SportyBet is only used to discover which markets actually exist.

    It is NOT used to create probability.
    """

    if not event:
        return []

    facts = None

    if use_data:
        fixture = find_fixture(event)

        if fixture:
            facts = get_facts(fixture)

    if not facts:
        _note(
            "No football evidence available; "
            "match rejected from evidence ranking."
        )
        return []

    try:
        markets = provider._event_markets_cached(
            event["id"]
        )
    except Exception as exc:
        _note(
            f"Could not load SportyBet markets: {exc}"
        )
        return []

    try:
        import smart_ticket

        candidates = smart_ticket.event_candidates(
            event,
            markets,
        )

    except Exception as exc:
        _note(
            f"Could not build SportyBet candidates: {exc}"
        )
        return []

    ranked = []

    for candidate in candidates:

        kind = str(
            candidate.get("kind")
            or ""
        ).lower()

        # User explicitly doesn't want DNB.
        if kind in {
            "dnb",
            "draw_no_bet",
        }:
            continue

        # No negative handicaps.
        if kind in {
            "handicap",
            "asian_handicap",
        }:
            line = _num(candidate.get("line"))

            if line is not None and line <= 0:
                continue

        odds = _candidate_odds(candidate)

        if odds is None or odds < MIN_ODDS:
            continue

        football_p = _candidate_probability(
            candidate,
            facts,
        )

        if football_p is None:
            continue

        # Weak football evidence is rejected.
        if football_p < REJECT_P:
            continue

        item = dict(candidate)

        item["football_probability"] = football_p
        item["probability"] = football_p
        item["confidence"] = confidence_label(
            football_p
        )
        item["football_reason"] = reason_for(
            candidate,
            facts,
            football_p,
        )
        item["football_facts"] = facts

        ranked.append(item)

    ranked.sort(
        key=lambda x: (
            x.get("football_probability", 0),
            x.get("odds", 0),
        ),
        reverse=True,
    )

    return ranked


def safest_for_leg(provider, event, use_data=True):
    ranked = _rank(
        provider,
        event,
        use_data,
    )

    if not ranked:
        return None

    return ranked[0]


def rebuild_safer(provider, event):
    """
    Rebuild a pick using football evidence.

    Returns None when the match is too weak.
    """

    return safest_for_leg(
        provider,
        event,
        use_data=True,
    )


# ============================================================
# EXTRA PUBLIC HELPERS
# ============================================================

def football_match_summary(event):
    """
    Useful for debugging / future frontend display.
    """

    facts = event_for(event)

    if not facts:
        return {
            "available": False,
            "message": NO_DATA,
        }

    model = _football_1x2_model(facts)

    return {
        "available": True,
        "home": facts.get("home"),
        "away": facts.get("away"),
        "home_probability": (
            model["home"]
            if model
            else None
        ),
        "draw_probability": (
            model["draw"]
            if model
            else None
        ),
        "away_probability": (
            model["away"]
            if model
            else None
        ),
        "data_quality": (
            model["quality"]
            if model
            else facts.get("data_quality", 0)
        ),
        "model_agreement": (
            model["agreement"]
            if model
            else None
        ),
        "lambda_home": facts.get(
            "lambda_home"
        ),
        "lambda_away": facts.get(
            "lambda_away"
        ),
        "digest": facts_digest(facts),
    }


def clear_cache():
    _facts_cache.clear()
    _fixture_index.clear()


# ============================================================
# STARTUP MESSAGE
# ============================================================

if __name__ == "__main__":
    print("SportyTips Football Evidence Engine")
    print("Football-first probability model loaded.")
    print("SportyBet odds are NOT used to create probabilities.")