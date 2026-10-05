"""
SportyTips Football Evidence Engine

Football evidence FIRST.
SportyBet is used only for:
    - market availability
    - odds
    - real booking-code creation

SportyBet odds NEVER create football probability.
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

ENRICH_MAX = int(os.getenv("FOOTBALL_ENRICH_MAX", "30"))
ENRICH_SECONDS = int(os.getenv("FOOTBALL_ENRICH_SECONDS", "45"))
MIN_QUOTA = int(os.getenv("FOOTBALL_MIN_QUOTA", "6"))

FACTS_CACHE_SECONDS = int(
    os.getenv(
        "FOOTBALL_FACTS_CACHE_SECONDS",
        str(3 * 3600)
    )
)

# Compatibility only.
# NEVER used to create football probability.
MIN_ODDS = float(
    os.getenv("FOOTBALL_MIN_ODDS", "1.01")
)

ELITE_P = 0.85
VERY_STRONG_P = 0.80
STRONG_P = 0.75
ACCEPTABLE_P = 0.70
WEAK_P = 0.65

REJECT_P = 0.65

FORM_WEIGHT = 0.22
GOALS_WEIGHT = 0.23
VENUE_WEIGHT = 0.15
H2H_WEIGHT = 0.07
API_MODEL_WEIGHT = 0.15
POISSON_WEIGHT = 0.18

FORM_RESULTS = 5
MAX_H2H = 8


# ============================================================
# CACHE
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


# ============================================================
# DIAGNOSTICS
# ============================================================

def reset_diag():
    global _diag, _started_at, _enriched_count

    _diag = []
    _started_at = time.time()
    _enriched_count = 0


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

    return (
        "Football model diagnostics: "
        + " | ".join(unique[-12:])
    )


# ============================================================
# HELPERS
# ============================================================

def _clean(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


def _num(value, default=None):

    try:
        if value is None or value == "":
            return default

        return float(value)

    except Exception:
        return default


def _clamp(
    value,
    low=0.0,
    high=1.0
):

    try:
        return max(
            low,
            min(high, float(value))
        )

    except Exception:
        return low


def _safe_average(
    values,
    default=None
):

    nums = []

    for value in values:

        n = _num(value)

        if n is not None:
            nums.append(n)

    if not nums:
        return default

    return sum(nums) / len(nums)


def _weighted_average(values):

    usable = []

    for value, weight in values:

        if value is None:
            continue

        if weight <= 0:
            continue

        usable.append(
            (
                float(value),
                float(weight)
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

    return (
        sum(
            value * weight
            for value, weight in usable
        )
        / total_weight
    )


# ============================================================
# FIXTURE LOOKUP
# ============================================================

def _event_datetime(event):

    kickoff = (
        event.get("kickoff")
        or event.get("startTime")
        or event.get("start_time")
    )

    if kickoff is None:
        return None

    try:

        if isinstance(
            kickoff,
            (int, float)
        ):

            return datetime.fromtimestamp(
                (
                    kickoff / 1000
                    if kickoff > 10_000_000_000
                    else kickoff
                ),
                tz=timezone.utc
            )

        dt = datetime.fromisoformat(
            str(kickoff).replace(
                "Z",
                "+00:00"
            )
        )

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:

        return None


def _fixtures_for(date_string):

    now = time.time()

    cached = _fixture_index.get(
        date_string
    )

    if cached:

        if (
            now - cached["time"]
            < 600
        ):

            return cached["fixtures"]

    try:

        fixtures = (
            bot.get_allowed_fixtures_cached(
                date_string
            )
            or []
        )

        _fixture_index[
            date_string
        ] = {
            "time": now,
            "fixtures": fixtures,
        }

        return fixtures

    except Exception as exc:

        _note(
            f"Fixture lookup failed: {exc}"
        )

        return []


def find_fixture(event):

    if not event:
        return None

    event_dt = _event_datetime(
        event
    )

    if not event_dt:
        return None

    date_string = (
        event_dt
        .astimezone(bot.LOCAL_TZ)
        .strftime("%Y-%m-%d")
    )

    fixtures = _fixtures_for(
        date_string
    )

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

            fixture_data = fixture.get(
                "fixture",
                {}
            )

            teams = fixture.get(
                "teams",
                {}
            )

            fixture_date = fixture_data.get(
                "date"
            )

            if not fixture_date:
                continue

            fixture_dt = datetime.fromisoformat(
                str(
                    fixture_date
                ).replace(
                    "Z",
                    "+00:00"
                )
            )

            if fixture_dt.tzinfo is None:

                fixture_dt = (
                    fixture_dt.replace(
                        tzinfo=timezone.utc
                    )
                )

            difference = abs(
                (
                    fixture_dt
                    - event_dt
                ).total_seconds()
            ) / 60

            if difference > 45:
                continue

            api_home = _clean(
                teams
                .get("home", {})
                .get("name")
            )

            api_away = _clean(
                teams
                .get("away", {})
                .get("name")
            )

            try:

                home_similarity = sp._sim(
                    home_name,
                    api_home
                )

                away_similarity = sp._sim(
                    away_name,
                    api_away
                )

            except Exception:

                home_similarity = (
                    1.0
                    if home_name.lower()
                    == api_home.lower()
                    else 0.0
                )

                away_similarity = (
                    1.0
                    if away_name.lower()
                    == api_away.lower()
                    else 0.0
                )

            score = (
                home_similarity
                + away_similarity
            ) / 2

            if score > best_score:

                best_score = score
                best = fixture

        except Exception:

            continue

    if (
        best is None
        or best_score < 0.72
    ):

        return None

    return best


# ============================================================
# API QUOTA
# ============================================================

def _api_remaining():

    try:

        return int(
            getattr(
                bot,
                "_api_remaining",
                100
            )
        )

    except Exception:

        return 100


# ============================================================
# FORM
# ============================================================

def _parse_form_string(form):

    if not form:
        return None

    values = []

    for char in str(form).upper():

        if char == "W":
            values.append(1.0)

        elif char == "D":
            values.append(0.5)

        elif char == "L":
            values.append(0.0)

    if not values:
        return None

    return (
        sum(values)
        / len(values)
    )


def _goal_total(goals, key):

    if not isinstance(
        goals,
        dict
    ):
        return None

    value = goals.get(key)

    if isinstance(
        value,
        dict
    ):

        return _num(
            value.get("total")
        )

    return _num(value)


def _record_points(record):

    if not isinstance(
        record,
        dict
    ):
        return None

    def extract(key, alternate=None):

        value = record.get(key)

        if isinstance(
            value,
            dict
        ):

            return _num(
                value.get("total")
            )

        if value is not None:
            return _num(value)

        if alternate:
            return _num(
                record.get(alternate)
            )

        return None

    wins = extract("wins")
    draws = extract("draws")
    losses = extract(
        "loses",
        "losses"
    )

    if (
        wins is None
        and draws is None
        and losses is None
    ):
        return None

    wins = wins or 0
    draws = draws or 0
    losses = losses or 0

    total = (
        wins
        + draws
        + losses
    )

    if total <= 0:
        return None

    return {
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "sample": total,
        "points_rate": (
            wins * 3 + draws
        ) / (
            total * 3
        ),
    }


# ============================================================
# FACT EXTRACTION
# ============================================================

def parse_facts(item):

    if not item:
        return None

    fixture = item.get(
        "fixture",
        {}
    )

    teams = item.get(
        "teams",
        {}
    )

    home = item.get(
        "home",
        {}
    ) or {}

    away = item.get(
        "away",
        {}
    ) or {}

    predictions = item.get(
        "predictions",
        {}
    ) or {}

    home_team = (
        teams.get(
            "home",
            {}
        )
        or {}
    )

    away_team = (
        teams.get(
            "away",
            {}
        )
        or {}
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

    home_id = (
        home_team.get("id")
        or home.get("id")
    )

    away_id = (
        away_team.get("id")
        or away.get("id")
    )

    # --------------------------------------------------------
    # FORM
    # --------------------------------------------------------

    home_last5 = (
        home.get(
            "last_5",
            {}
        )
        or {}
    )

    away_last5 = (
        away.get(
            "last_5",
            {}
        )
        or {}
    )

    home_form_raw = (
        home_last5.get("form")
        or home.get(
            "league",
            {}
        ).get("form")
        or home.get("form")
    )

    away_form_raw = (
        away_last5.get("form")
        or away.get(
            "league",
            {}
        ).get("form")
        or away.get("form")
    )

    home_form = _parse_form_string(
        home_form_raw
    )

    away_form = _parse_form_string(
        away_form_raw
    )

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    home_goals = (
        home_last5.get(
            "goals",
            {}
        )
        or {}
    )

    away_goals = (
        away_last5.get(
            "goals",
            {}
        )
        or {}
    )

    home_gf = _goal_total(
        home_goals,
        "for"
    )

    home_ga = _goal_total(
        home_goals,
        "against"
    )

    away_gf = _goal_total(
        away_goals,
        "for"
    )

    away_ga = _goal_total(
        away_goals,
        "against"
    )

    if (
        home_gf is not None
        and home_gf > 5
    ):
        home_gf /= FORM_RESULTS

    if (
        home_ga is not None
        and home_ga > 5
    ):
        home_ga /= FORM_RESULTS

    if (
        away_gf is not None
        and away_gf > 5
    ):
        away_gf /= FORM_RESULTS

    if (
        away_ga is not None
        and away_ga > 5
    ):
        away_ga /= FORM_RESULTS

    # --------------------------------------------------------
    # HOME / AWAY RECORD
    # --------------------------------------------------------

    home_league = (
        home.get(
            "league",
            {}
        )
        or {}
    )

    away_league = (
        away.get(
            "league",
            {}
        )
        or {}
    )

    home_fixtures = (
        home_league.get(
            "fixtures",
            {}
        )
        or {}
    )

    away_fixtures = (
        away_league.get(
            "fixtures",
            {}
        )
        or {}
    )

    home_venue = _record_points(
        home_fixtures.get(
            "home",
            {}
        )
    )

    away_venue = _record_points(
        away_fixtures.get(
            "away",
            {}
        )
    )

    # --------------------------------------------------------
    # API MODEL
    # --------------------------------------------------------

    percent = (
        predictions.get(
            "percent",
            {}
        )
        or {}
    )

    api_home = _num(
        percent.get("home")
    )

    api_draw = _num(
        percent.get("draw")
    )

    api_away = _num(
        percent.get("away")
    )

    if api_home is not None:
        api_home /= 100

    if api_draw is not None:
        api_draw /= 100

    if api_away is not None:
        api_away /= 100

    # Compatibility with old smart_ticket.
    api_model = {
        "home": api_home,
        "draw": api_draw,
        "away": api_away,
    }

    # --------------------------------------------------------
    # H2H
    # --------------------------------------------------------

    h2h = (
        predictions.get("h2h")
        or []
    )

    if not isinstance(
        h2h,
        list
    ):
        h2h = []

    h2h = h2h[:MAX_H2H]

    h2h_home_wins = 0
    h2h_draws = 0
    h2h_away_wins = 0

    h2h_goals = []
    h2h_btts = 0

    for match in h2h:

        try:

            match_teams = (
                match.get(
                    "teams",
                    {}
                )
                or {}
            )

            match_goals = (
                match.get(
                    "goals",
                    {}
                )
                or {}
            )

            match_home = (
                match_teams.get(
                    "home",
                    {}
                )
                or {}
            )

            match_away = (
                match_teams.get(
                    "away",
                    {}
                )
                or {}
            )

            hg = _num(
                match_goals.get(
                    "home"
                )
            )

            ag = _num(
                match_goals.get(
                    "away"
                )
            )

            if (
                hg is None
                or ag is None
            ):
                continue

            h2h_goals.append(
                hg + ag
            )

            if (
                hg > 0
                and ag > 0
            ):
                h2h_btts += 1

            h_id = match_home.get(
                "id"
            )

            a_id = match_away.get(
                "id"
            )

            if (
                h_id == home_id
                and a_id == away_id
            ):

                if hg > ag:
                    h2h_home_wins += 1

                elif hg == ag:
                    h2h_draws += 1

                else:
                    h2h_away_wins += 1

            elif (
                h_id == away_id
                and a_id == home_id
            ):

                if hg > ag:
                    h2h_away_wins += 1

                elif hg == ag:
                    h2h_draws += 1

                else:
                    h2h_home_wins += 1

        except Exception:

            continue

    h2h_sample = (
        h2h_home_wins
        + h2h_draws
        + h2h_away_wins
    )

    h2h_home_rate = None
    h2h_away_rate = None

    if h2h_sample:

        h2h_home_rate = (
            h2h_home_wins
            + 0.5 * h2h_draws
        ) / h2h_sample

        h2h_away_rate = (
            h2h_away_wins
            + 0.5 * h2h_draws
        ) / h2h_sample

    h2h_avg_goals = _safe_average(
        h2h_goals
    )

    h2h_btts_rate = (
        h2h_btts / h2h_sample
        if h2h_sample
        else None
    )

    # --------------------------------------------------------
    # EXPECTED GOALS
    # --------------------------------------------------------

    lambda_home = None
    lambda_away = None

    if (
        home_gf is not None
        and away_ga is not None
    ):

        lambda_home = (
            home_gf
            + away_ga
        ) / 2

    if (
        away_gf is not None
        and home_ga is not None
    ):

        lambda_away = (
            away_gf
            + home_ga
        ) / 2

    if lambda_home is not None:

        lambda_home = max(
            0.15,
            min(
                4.50,
                lambda_home
            )
        )

    if lambda_away is not None:

        lambda_away = max(
            0.15,
            min(
                4.50,
                lambda_away
            )
        )

    # --------------------------------------------------------
    # DATA QUALITY
    # --------------------------------------------------------

    checks = [

        home_form is not None
        and away_form is not None,

        home_gf is not None
        and home_ga is not None,

        away_gf is not None
        and away_ga is not None,

        home_venue is not None,

        away_venue is not None,

        api_home is not None
        and api_draw is not None
        and api_away is not None,

        lambda_home is not None
        and lambda_away is not None,

        h2h_sample >= 2,
    ]

    data_quality = (
        sum(
            bool(x)
            for x in checks
        )
        / len(checks)
    )

    return {

        "fixture_id":
            fixture.get("id"),

        "home":
            home_name,

        "away":
            away_name,

        "home_id":
            home_id,

        "away_id":
            away_id,

        "home_form_raw":
            home_form_raw,

        "away_form_raw":
            away_form_raw,

        "home_form":
            home_form,

        "away_form":
            away_form,

        "home_gf":
            home_gf,

        "home_ga":
            home_ga,

        "away_gf":
            away_gf,

        "away_ga":
            away_ga,

        "home_venue":
            home_venue,

        "away_venue":
            away_venue,

        "api_home":
            api_home,

        "api_draw":
            api_draw,

        "api_away":
            api_away,

        # Compatibility field.
        "api_model":
            api_model,

        "h2h_sample":
            h2h_sample,

        # Compatibility field expected by old smart_ticket.
        "h2h_count":
            h2h_sample,

        "h2h_home_wins":
            h2h_home_wins,

        "h2h_draws":
            h2h_draws,

        "h2h_away_wins":
            h2h_away_wins,

        "h2h_home_rate":
            h2h_home_rate,

        "h2h_away_rate":
            h2h_away_rate,

        "h2h_avg_goals":
            h2h_avg_goals,

        "h2h_btts_rate":
            h2h_btts_rate,

        "lambda_home":
            lambda_home,

        "lambda_away":
            lambda_away,

        "data_quality":
            data_quality,

        # Compatibility fields expected by old code.
        "home_home":
            home_venue,

        "away_away":
            away_venue,

        "raw":
            item,
    }


# ============================================================
# GET FACTS
# ============================================================

def get_facts(fixture):

    global _enriched_count

    if not fixture:
        return None

    fixture_id = (
        fixture.get(
            "fixture",
            {}
        ).get("id")
        or fixture.get("id")
    )

    if not fixture_id:
        return None

    now = time.time()

    cached = _facts_cache.get(
        fixture_id
    )

    if cached:

        if (
            now - cached["time"]
            < FACTS_CACHE_SECONDS
        ):

            return cached["facts"]

    if (
        _api_remaining()
        < MIN_QUOTA
    ):

        _note(
            "API-Football quota is low; "
            "skipping new enrichment."
        )

        return None

    if (
        _enriched_count
        >= ENRICH_MAX
    ):

        _note(
            f"Football enrichment limit "
            f"reached ({ENRICH_MAX})."
        )

        return None

    if (
        time.time()
        - _started_at
        > ENRICH_SECONDS
    ):

        _note(
            "Football enrichment "
            "time limit reached."
        )

        return None

    try:

        result = bot.football_request(
            "/predictions",
            {
                "fixture":
                    fixture_id
            }
        )

        _enriched_count += 1

        response = (
            result.get(
                "response",
                []
            )
        )

        if not response:

            _note(
                f"No API-Football prediction "
                f"data for fixture {fixture_id}."
            )

            return None

        facts = parse_facts(
            response[0]
        )

        if not facts:
            return None

        _facts_cache[
            fixture_id
        ] = {
            "time":
                time.time(),

            "facts":
                facts,
        }

        return facts

    except Exception as exc:

        _enriched_count += 1

        _note(
            f"Football data request failed "
            f"for {fixture_id}: {exc}"
        )

        return None


# ============================================================
# POISSON
# ============================================================

def _poisson_probability(
    lam,
    goals
):

    if lam is None:
        return 0.0

    try:

        return (
            math.exp(-lam)
            * lam ** goals
            / math.factorial(goals)
        )

    except Exception:

        return 0.0


def _score_matrix(
    lambda_home,
    lambda_away,
    max_goals=10
):

    if (
        lambda_home is None
        or lambda_away is None
    ):
        return []

    matrix = []

    for home_goals in range(
        max_goals + 1
    ):

        row = []

        home_probability = (
            _poisson_probability(
                lambda_home,
                home_goals
            )
        )

        for away_goals in range(
            max_goals + 1
        ):

            away_probability = (
                _poisson_probability(
                    lambda_away,
                    away_goals
                )
            )

            row.append(
                home_probability
                * away_probability
            )

        matrix.append(row)

    return matrix


def _poisson_win_probs(
    lambda_home,
    lambda_away
):

    matrix = _score_matrix(
        lambda_home,
        lambda_away
    )

    if not matrix:
        return None

    home = 0.0
    draw = 0.0
    away = 0.0

    for hg, row in enumerate(matrix):

        for ag, probability in enumerate(
            row
        ):

            if hg > ag:
                home += probability

            elif hg == ag:
                draw += probability

            else:
                away += probability

    total = (
        home
        + draw
        + away
    )

    if total <= 0:
        return None

    return {

        "home":
            home / total,

        "draw":
            draw / total,

        "away":
            away / total,
    }


def _poisson_over(
    lambda_home,
    lambda_away,
    line
):

    if (
        lambda_home is None
        or lambda_away is None
    ):
        return None

    line = _num(line)

    if line is None:
        return None

    total_lambda = (
        lambda_home
        + lambda_away
    )

    # Over 0.5 = 1+
    # Over 1.5 = 2+
    # Over 2.5 = 3+
    # Over 3.5 = 4+

    max_under = int(
        math.floor(line)
    )

    under = 0.0

    for goals in range(
        max_under + 1
    ):

        under += (
            _poisson_probability(
                total_lambda,
                goals
            )
        )

    return _clamp(
        1.0 - under
    )


def _poisson_team_goals(
    lam,
    line
):

    if lam is None:
        return None

    line = _num(line)

    if line is None:
        return None

    max_under = int(
        math.floor(line)
    )

    under = 0.0

    for goals in range(
        max_under + 1
    ):

        under += (
            _poisson_probability(
                lam,
                goals
            )
        )

    return _clamp(
        1.0 - under
    )


def _poisson_btts(
    lambda_home,
    lambda_away
):

    if (
        lambda_home is None
        or lambda_away is None
    ):
        return None

    home_zero = math.exp(
        -lambda_home
    )

    away_zero = math.exp(
        -lambda_away
    )

    both_zero = (
        home_zero
        * away_zero
    )

    return _clamp(
        1
        - home_zero
        - away_zero
        + both_zero
    )


# ============================================================
# EVIDENCE COMPONENTS
# ============================================================

def _form_strength(facts):

    home = facts.get(
        "home_form"
    )

    away = facts.get(
        "away_form"
    )

    if (
        home is None
        or away is None
    ):
        return None

    total = home + away

    if total <= 0:
        return 0.5

    return _clamp(
        home / total,
        0.05,
        0.95
    )


def _goal_strength(facts):

    hg = facts.get(
        "home_gf"
    )

    hga = facts.get(
        "home_ga"
    )

    ag = facts.get(
        "away_gf"
    )

    aga = facts.get(
        "away_ga"
    )

    if None in (
        hg,
        hga,
        ag,
        aga
    ):
        return None

    home_strength = _clamp(
        0.5
        + (hg - hga) / 4,
        0.05,
        0.95
    )

    away_strength = _clamp(
        0.5
        + (ag - aga) / 4,
        0.05,
        0.95
    )

    total = (
        home_strength
        + away_strength
    )

    if total <= 0:
        return 0.5

    return _clamp(
        home_strength / total,
        0.05,
        0.95
    )


def _venue_strength(facts):

    home = facts.get(
        "home_venue"
    )

    away = facts.get(
        "away_venue"
    )

    if not home or not away:
        return None

    hp = _num(
        home.get(
            "points_rate"
        )
    )

    ap = _num(
        away.get(
            "points_rate"
        )
    )

    if hp is None or ap is None:
        return None

    total = hp + ap

    if total <= 0:
        return 0.5

    return _clamp(
        hp / total,
        0.05,
        0.95
    )


def _h2h_strength(facts):

    sample = facts.get(
        "h2h_sample",
        0
    )

    if sample < 2:
        return None

    home = facts.get(
        "h2h_home_rate"
    )

    away = facts.get(
        "h2h_away_rate"
    )

    if (
        home is None
        or away is None
    ):
        return None

    total = home + away

    if total <= 0:
        return 0.5

    raw = home / total

    reliability = _clamp(
        sample / MAX_H2H,
        0.15,
        1.0
    )

    return _clamp(
        0.5
        + (
            raw - 0.5
        ) * reliability,
        0.05,
        0.95
    )


def _api_model_strength(facts):

    home = facts.get(
        "api_home"
    )

    away = facts.get(
        "api_away"
    )

    if (
        home is None
        or away is None
    ):
        return None

    total = home + away

    if total <= 0:
        return None

    return _clamp(
        home / total,
        0.05,
        0.95
    )


def _agreement(values):

    values = [
        float(value)
        for value in values
        if value is not None
    ]

    if len(values) < 2:
        return 0.55

    spread = (
        max(values)
        - min(values)
    )

    return _clamp(
        1.0
        - spread / 0.40,
        0.20,
        1.0
    )


# ============================================================
# MAIN FOOTBALL MODEL
# ============================================================

def _football_1x2_model(facts):

    if not facts:
        return None

    form = _form_strength(
        facts
    )

    goals = _goal_strength(
        facts
    )

    venue = _venue_strength(
        facts
    )

    h2h = _h2h_strength(
        facts
    )

    api_model = _api_model_strength(
        facts
    )

    poisson = _poisson_win_probs(
        facts.get(
            "lambda_home"
        ),
        facts.get(
            "lambda_away"
        )
    )

    poisson_home = (
        poisson.get("home")
        if poisson
        else None
    )

    weighted = _weighted_average([

        (
            form,
            FORM_WEIGHT
        ),

        (
            goals,
            GOALS_WEIGHT
        ),

        (
            venue,
            VENUE_WEIGHT
        ),

        (
            h2h,
            H2H_WEIGHT
        ),

        (
            api_model,
            API_MODEL_WEIGHT
        ),

        (
            poisson_home,
            POISSON_WEIGHT
        ),
    ])

    if weighted is None:
        return None

    quality = _clamp(
        facts.get(
            "data_quality",
            0
        )
    )

    agreement = _agreement([

        form,
        goals,
        venue,
        h2h,
        api_model,
        poisson_home
    ])

    confidence_factor = (
        0.65
        + 0.20 * quality
        + 0.15 * agreement
    )

    home_probability = _clamp(
        0.5
        + (
            weighted
            - 0.5
        )
        * confidence_factor
    )

    draw_probability = (
        poisson.get(
            "draw"
        )
        if poisson
        else 0.25
    )

    draw_probability = _clamp(
        draw_probability,
        0.05,
        0.60
    )

    away_probability = max(
        0.001,
        1.0
        - home_probability
        - draw_probability
    )

    total = (
        home_probability
        + draw_probability
        + away_probability
    )

    return {

        "home":
            home_probability / total,

        "draw":
            draw_probability / total,

        "away":
            away_probability / total,

        "form":
            form,

        "goals":
            goals,

        "venue":
            venue,

        "h2h":
            h2h,

        "api_model":
            api_model,

        "poisson":
            poisson_home,

        "agreement":
            agreement,

        "quality":
            quality,
    }


# ============================================================
# 1UP / 2UP
# ============================================================

def _lead_probability(
    lambda_home,
    lambda_away,
    side,
    required_lead
):

    matrix = _score_matrix(
        lambda_home,
        lambda_away
    )

    if not matrix:
        return None

    probability = 0.0

    for hg, row in enumerate(
        matrix
    ):

        for ag, value in enumerate(
            row
        ):

            margin = (
                hg - ag
                if side == "home"
                else ag - hg
            )

            if margin >= required_lead:
                probability += value

    # Conservative adjustment because
    # final-score margin isn't exactly
    # the same thing as early settlement.
    return _clamp(
        probability * 0.92
    )


# ============================================================
# POSITIVE HANDICAP
# ============================================================

def _handicap_probability(
    facts,
    side,
    line
):

    matrix = _score_matrix(
        facts.get(
            "lambda_home"
        ),
        facts.get(
            "lambda_away"
        )
    )

    if not matrix:
        return None

    line = _num(line)

    if line is None:
        return None

    probability = 0.0

    for hg, row in enumerate(
        matrix
    ):

        for ag, value in enumerate(
            row
        ):

            margin = (
                hg - ag
                if side == "home"
                else ag - hg
            )

            adjusted = (
                margin + line
            )

            if adjusted > 0:

                probability += value

            elif adjusted == 0:

                probability += (
                    value * 0.50
                )

    return _clamp(
        probability
    )


# ============================================================
# MARKET PROBABILITY
# ============================================================

def _football_probability(
    candidate,
    facts
):

    if (
        not candidate
        or not facts
    ):
        return None

    kind = str(
        candidate.get(
            "kind"
        )
        or candidate.get(
            "market_kind"
        )
        or ""
    ).lower()

    side = str(
        candidate.get(
            "side"
        )
        or candidate.get(
            "selection"
        )
        or candidate.get(
            "team_side"
        )
        or ""
    ).lower()

    line = _num(
        candidate.get(
            "line"
        )
    )

    model = _football_1x2_model(
        facts
    )

    if not model:
        return None

    # --------------------------------------------------------
    # WIN / 1UP / 2UP
    # --------------------------------------------------------

    if kind in {
        "win",
        "1x2",
        "straight_win",
        "up",
        "1up",
        "2up"
    }:

        if side in {
            "home",
            "h",
            "1"
        }:

            selected = "home"

        elif side in {
            "away",
            "a",
            "2"
        }:

            selected = "away"

        else:

            return None

        if kind in {
            "1up",
            "2up"
        }:

            return _lead_probability(

                facts.get(
                    "lambda_home"
                ),

                facts.get(
                    "lambda_away"
                ),

                selected,

                (
                    1
                    if kind == "1up"
                    else 2
                )
            )

        return model[
            selected
        ]

    # --------------------------------------------------------
    # EITHER HALF
    # --------------------------------------------------------

    if kind in {
        "either_half",
        "win_either_half",
        "team_win_either_half"
    }:

        if side in {
            "home",
            "h",
            "1"
        }:

            team_lambda = facts.get(
                "lambda_home"
            )

            opponent_lambda = facts.get(
                "lambda_away"
            )

            team_key = "home"

        elif side in {
            "away",
            "a",
            "2"
        }:

            team_lambda = facts.get(
                "lambda_away"
            )

            opponent_lambda = facts.get(
                "lambda_home"
            )

            team_key = "away"

        else:

            return None

        if (
            team_lambda is None
            or opponent_lambda is None
        ):
            return None

        # Half-level approximation.
        # We split expected goals between
        # the two halves.
        first_team = (
            team_lambda * 0.45
        )

        first_opponent = (
            opponent_lambda * 0.45
        )

        second_team = (
            team_lambda * 0.55
        )

        second_opponent = (
            opponent_lambda * 0.55
        )

        first = _poisson_win_probs(
            first_team,
            first_opponent
        )

        second = _poisson_win_probs(
            second_team,
            second_opponent
        )

        if (
            not first
            or not second
        ):
            return None

        first_win = first[
            team_key
        ]

        second_win = second[
            team_key
        ]

        return _clamp(
            1
            - (
                (1 - first_win)
                * (1 - second_win)
            )
        )

    # --------------------------------------------------------
    # 12 / DOUBLE CHANCE
    # --------------------------------------------------------

    if kind in {
        "dc",
        "double_chance"
    }:

        if side in {
            "12",
            "homeaway",
            "home_away"
        }:

            return _clamp(
                model["home"]
                + model["away"]
            )

        if side in {
            "1x",
            "homedraw"
        }:

            return _clamp(
                model["home"]
                + model["draw"]
            )

        if side in {
            "x2",
            "drawaway"
        }:

            return _clamp(
                model["draw"]
                + model["away"]
            )

        return None

    # --------------------------------------------------------
    # OVER
    # --------------------------------------------------------

    if kind in {
        "over",
        "over15",
        "goals",
        "total"
    }:

        return _poisson_over(

            facts.get(
                "lambda_home"
            ),

            facts.get(
                "lambda_away"
            ),

            (
                line
                if line is not None
                else 1.5
            )
        )

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    if kind in {
        "btts",
        "btts_yes"
    }:

        return _poisson_btts(

            facts.get(
                "lambda_home"
            ),

            facts.get(
                "lambda_away"
            )
        )

    # --------------------------------------------------------
    # TEAM GOALS
    # --------------------------------------------------------

    if kind in {
        "team_goals",
        "home_team_goals",
        "away_team_goals"
    }:

        selected_line = (
            line
            if line is not None
            else 0.5
        )

        if (
            kind == "away_team_goals"
            or side in {
                "away",
                "a",
                "2"
            }
        ):

            lam = facts.get(
                "lambda_away"
            )

        else:

            lam = facts.get(
                "lambda_home"
            )

        return _poisson_team_goals(
            lam,
            selected_line
        )

    # --------------------------------------------------------
    # POSITIVE HANDICAP
    # --------------------------------------------------------

    if kind in {
        "handicap",
        "asian_handicap",
        "positive_handicap",
        "positive_asian_handicap"
    }:

        if (
            line is None
            or line <= 0
        ):
            return None

        if side in {
            "home",
            "h",
            "1"
        }:

            handicap_side = "home"

        elif side in {
            "away",
            "a",
            "2"
        }:

            handicap_side = "away"

        else:

            return None

        return _handicap_probability(
            facts,
            handicap_side,
            line
        )

    # --------------------------------------------------------
    # CORNERS
    # --------------------------------------------------------

    if kind in {
        "corners",
        "corners_1h",
        "corner"
    }:

        # Deliberately return None.
        # No reliable corner dataset has been
        # supplied to the football model.
        return None

    return None


# ============================================================
# PUBLIC PROBABILITY
# ============================================================

def adjusted_p(
    candidate,
    facts
):

    if not facts:
        return None

    probability = _football_probability(
        candidate,
        facts
    )

    if probability is None:
        return None

    model = _football_1x2_model(
        facts
    )

    if not model:
        return None

    quality = model.get(
        "quality",
        0
    )

    agreement = model.get(
        "agreement",
        0
    )

    # Small calibration penalty.
    # This is NOT related to SportyBet odds.
    penalty = (
        0.90
        + 0.07 * quality
        + 0.03 * agreement
    )

    return _clamp(
        probability * penalty,
        0.01,
        0.97
    )


# ============================================================
# COMPATIBILITY HELPERS
# ============================================================

def _facts_quality(facts):

    if not facts:
        return 0.0

    return _clamp(
        facts.get(
            "data_quality",
            0
        )
    )


def _model_disagreement_penalty(
    facts,
    candidate=None
):

    model = _football_1x2_model(
        facts
    )

    if not model:
        return 0.0

    agreement = model.get(
        "agreement",
        0.55
    )

    if agreement >= 0.75:
        return 0.0

    if agreement >= 0.60:
        return 0.02

    if agreement >= 0.45:
        return 0.05

    return 0.08


# ============================================================
# CONFIDENCE
# ============================================================

def confidence_label(
    probability
):

    if probability is None:
        return "REJECT"

    p = float(
        probability
    )

    if p >= ELITE_P:
        return "ELITE"

    if p >= VERY_STRONG_P:
        return "VERY STRONG"

    if p >= STRONG_P:
        return "STRONG"

    if p >= ACCEPTABLE_P:
        return "ACCEPTABLE"

    if p >= WEAK_P:
        return "WEAK"

    return "REJECT"


# ============================================================
# MARKET-SPECIFIC REASON
# ============================================================

def reason_for(
    candidate,
    facts,
    probability=None
):

    if not facts:
        return NO_DATA

    if probability is None:

        probability = adjusted_p(
            candidate,
            facts
        )

    if probability is None:
        return NO_DATA

    kind = str(
        candidate.get(
            "kind"
        )
        or ""
    ).lower()

    side = str(
        candidate.get(
            "side"
        )
        or ""
    ).lower()

    home = facts.get(
        "home",
        "Home"
    )

    away = facts.get(
        "away",
        "Away"
    )

    home_form = facts.get(
        "home_form"
    )

    away_form = facts.get(
        "away_form"
    )

    home_gf = facts.get(
        "home_gf"
    )

    home_ga = facts.get(
        "home_ga"
    )

    away_gf = facts.get(
        "away_gf"
    )

    away_ga = facts.get(
        "away_ga"
    )

    reasons = []

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    if kind in {
        "over",
        "over15",
        "goals",
        "total"
    }:

        lh = facts.get(
            "lambda_home"
        )

        la = facts.get(
            "lambda_away"
        )

        if (
            lh is not None
            and la is not None
        ):

            total = lh + la

            if total >= 2.40:

                reasons.append(
                    "the goal model expects an open match"
                )

            elif total >= 2.10:

                reasons.append(
                    "the goal model supports a reasonable scoring environment"
                )

        if (
            home_gf is not None
            and away_gf is not None
            and home_gf >= 1.20
            and away_gf >= 1.00
        ):

            reasons.append(
                "both teams have shown useful recent scoring output"
            )

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    elif kind in {
        "btts",
        "btts_yes"
    }:

        if (
            home_gf is not None
            and away_gf is not None
            and home_gf >= 1.0
            and away_gf >= 1.0
        ):

            reasons.append(
                "both teams have a recent scoring profile"
            )

        btts_rate = facts.get(
            "h2h_btts_rate"
        )

        if (
            btts_rate is not None
            and btts_rate >= 0.60
        ):

            reasons.append(
                "H2H meetings have frequently produced goals from both sides"
            )

    # --------------------------------------------------------
    # TEAM GOALS
    # --------------------------------------------------------

    elif kind in {
        "team_goals",
        "home_team_goals",
        "away_team_goals"
    }:

        if side in {
            "away",
            "a",
            "2"
        }:

            team = away
            gf = away_gf

        else:

            team = home
            gf = home_gf

        if (
            gf is not None
            and gf >= 1.20
        ):

            reasons.append(
                f"{team} has shown strong recent scoring output"
            )

        lam = (
            facts.get(
                "lambda_away"
            )
            if side in {
                "away",
                "a",
                "2"
            }
            else facts.get(
                "lambda_home"
            )
        )

        if (
            lam is not None
            and lam >= 1.30
        ):

            reasons.append(
                f"{team} has a strong expected-goals projection"
            )

    # --------------------------------------------------------
    # WIN / 1UP / 2UP / EITHER HALF
    # --------------------------------------------------------

    elif kind in {
        "win",
        "1x2",
        "straight_win",
        "up",
        "1up",
        "2up",
        "either_half",
        "win_either_half",
        "team_win_either_half"
    }:

        if side in {
            "home",
            "h",
            "1"
        }:

            team = home
            team_form = home_form
            opponent_form = away_form
            team_gf = home_gf
            team_ga = home_ga
            venue = facts.get(
                "home_venue"
            )
            opponent_venue = facts.get(
                "away_venue"
            )

        else:

            team = away
            team_form = away_form
            opponent_form = home_form
            team_gf = away_gf
            team_ga = away_ga
            venue = facts.get(
                "away_venue"
            )
            opponent_venue = facts.get(
                "home_venue"
            )

        if (
            team_form is not None
            and opponent_form is not None
            and team_form
            > opponent_form + 0.10
        ):

            reasons.append(
                f"{team} has the stronger recent form"
            )

        if (
            team_gf is not None
            and team_ga is not None
            and team_gf
            > team_ga + 0.25
        ):

            reasons.append(
                f"{team} has the stronger recent goal profile"
            )

        if (
            venue
            and opponent_venue
        ):

            vp = venue.get(
                "points_rate"
            )

            op = opponent_venue.get(
                "points_rate"
            )

            if (
                vp is not None
                and op is not None
                and vp > op + 0.15
            ):

                reasons.append(
                    f"{team} has the stronger relevant venue record"
                )

    # --------------------------------------------------------
    # 12
    # --------------------------------------------------------

    elif kind in {
        "dc",
        "double_chance"
    }:

        reasons.append(
            "the football model rates a home or away win more strongly than a draw"
        )

    # --------------------------------------------------------
    # POSITIVE HANDICAP
    # --------------------------------------------------------

    elif kind in {
        "handicap",
        "asian_handicap",
        "positive_handicap",
        "positive_asian_handicap"
    }:

        reasons.append(
            "the goal-margin model gives the selected side useful protection"
        )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    if not reasons:

        if (
            home_form is not None
            and away_form is not None
        ):

            if home_form > away_form + 0.10:

                reasons.append(
                    f"{home} has the stronger recent form"
                )

            elif away_form > home_form + 0.10:

                reasons.append(
                    f"{away} has the stronger recent form"
                )

    if not reasons:

        reasons.append(
            "the available football evidence supports the selected market"
        )

    quality = _facts_quality(
        facts
    )

    model = _football_1x2_model(
        facts
    )

    agreement = (
        model.get(
            "agreement",
            0
        )
        if model
        else 0
    )

    reasons.append(
        f"football model {probability * 100:.1f}%"
    )

    reasons.append(
        f"data quality {quality * 100:.0f}%"
    )

    if agreement < 0.55:

        reasons.append(
            "model disagreement lowers confidence"
        )

    return (
        "; ".join(
            reasons
        )
        + "."
    )


# ============================================================
# PUBLIC HELPERS
# ============================================================

def event_for(event):

    fixture = find_fixture(
        event
    )

    if not fixture:
        return None

    return get_facts(
        fixture
    )


def classify_leg(candidate):

    if not candidate:
        return "unknown"

    kind = str(
        candidate.get(
            "kind"
        )
        or ""
    ).lower()

    if kind in {
        "1up",
        "2up",
        "up",
        "win",
        "straight_win",
        "either_half",
        "win_either_half",
        "team_win_either_half"
    }:

        return "up"

    if kind in {
        "over",
        "over15",
        "goals",
        "team_goals",
        "btts",
        "btts_yes"
    }:

        return "goals"

    if kind in {
        "dc",
        "double_chance"
    }:

        return "dc"

    if kind in {
        "corners",
        "corners_1h"
    }:

        return "corners"

    if kind in {
        "handicap",
        "asian_handicap",
        "positive_handicap",
        "positive_asian_handicap"
    }:

        return "handicap"

    return (
        kind
        or "unknown"
    )


def why(
    event,
    candidate
):

    facts = event_for(
        event
    )

    if not facts:
        return NO_DATA

    probability = adjusted_p(
        candidate,
        facts
    )

    return reason_for(
        candidate,
        facts,
        probability
    )


def football_match_summary(
    event
):

    facts = event_for(
        event
    )

    if not facts:

        return {
            "available":
                False,

            "message":
                NO_DATA,
        }

    model = _football_1x2_model(
        facts
    )

    return {

        "available":
            True,

        "home":
            facts.get("home"),

        "away":
            facts.get("away"),

        "home_probability":
            model["home"]
            if model
            else None,

        "draw_probability":
            model["draw"]
            if model
            else None,

        "away_probability":
            model["away"]
            if model
            else None,

        "data_quality":
            model["quality"]
            if model
            else facts.get(
                "data_quality",
                0
            ),

        "model_agreement":
            model["agreement"]
            if model
            else None,

        "lambda_home":
            facts.get(
                "lambda_home"
            ),

        "lambda_away":
            facts.get(
                "lambda_away"
            ),
    }


def facts_digest(facts):

    if not facts:
        return NO_DATA

    home = facts.get(
        "home",
        "Home"
    )

    away = facts.get(
        "away",
        "Away"
    )

    parts = []

    hf = facts.get(
        "home_form_raw"
    )

    af = facts.get(
        "away_form_raw"
    )

    if hf or af:

        parts.append(
            f"Form: {home} {hf or '?'} / "
            f"{away} {af or '?'}"
        )

    lh = facts.get(
        "lambda_home"
    )

    la = facts.get(
        "lambda_away"
    )

    if (
        lh is not None
        and la is not None
    ):

        parts.append(
            f"Expected goals: "
            f"{home} {lh:.2f}, "
            f"{away} {la:.2f}"
        )

    model = _football_1x2_model(
        facts
    )

    if model:

        parts.append(
            f"Model: "
            f"{home} {model['home'] * 100:.1f}%, "
            f"Draw {model['draw'] * 100:.1f}%, "
            f"{away} {model['away'] * 100:.1f}%"
        )

        parts.append(
            f"Data quality "
            f"{model['quality'] * 100:.0f}%"
        )

        parts.append(
            f"Agreement "
            f"{model['agreement'] * 100:.0f}%"
        )

    return " | ".join(
        parts
    )


# ============================================================
# CACHE RESET
# ============================================================

def clear_cache():

    global _enriched_count
    global _started_at

    _facts_cache.clear()
    _fixture_index.clear()

    _enriched_count = 0
    _started_at = time.time()


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":

    print(
        "SportyTips Football Evidence Engine"
    )

    print(
        "Football-first model loaded."
    )

    print(
        "SportyBet odds NEVER create football probability."
    )