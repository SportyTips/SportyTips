"""
SportyTips Smart SportyBet ticket builder.

ARCHITECTURE
------------

OLD:
    SportyBet odds
        ↓
    implied probability
        ↓
    pick

NEW:
    SportyBet fixtures
        ↓
    Football_data.py
        ↓
    football evidence
        ↓
    football probability
        ↓
    evidence quality / disagreement checks
        ↓
    reject weak selections
        ↓
    SportyBet market availability
        ↓
    SportyBet odds
        ↓
    ticket
        ↓
    REAL SportyBet booking code

IMPORTANT
---------
- No fake booking codes.
- Booking codes come from SportyBet provider.
- No DNB.
- No negative handicaps.
- No Under selections.
- No yellow-card selections.
- No odds-derived football probability.
- Football_data.py is the prediction engine.
- SportyBet is only used for market availability, odds and booking codes.

SUPPORTED
---------
- 1UP / 2UP
- Over 0.5 / 1.5 / 2.5 / 3.5 goals
- BTTS Yes
- Team Goals Over 0.5 / 1.5
- Corners when Football_data.py can support them
- Positive handicap
- Positive Asian handicap
- Double Chance 12 only when explicitly allowed by the football model

STRAIGHT WIN
------------
Straight Win = 1UP + 2UP only.

The ticket builder NEVER creates a prediction from SportyBet odds.
"""

import html
import math
import re
import threading
import time
import traceback
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FutureTimeout,
)
from datetime import datetime, timedelta, timezone

import main as bot
import sportybet_provider as sp

try:
    import football_data as fd
except Exception as e:
    fd = None
    print("FOOTBALL_DATA IMPORT ERROR:", repr(e))


BRAND = "SPORTYTIPS"


# ============================================================
# SPEED / SAFETY SETTINGS
# ============================================================

MAX_DETAIL_EVENTS = 10
STRAIGHT_DETAIL_EVENTS = 40

DETAIL_WORKERS = 5
DETAIL_SECONDS = 10

MAX_CACHED_MATCHES = 150
MAX_PAGES = 4

MAX_LEGS = 30
MAX_OPTIONS_PER_MATCH = 5

GROUP_LIMIT = 80
OVERSHOOT = 0.06

# Football-data enrichment controls.
#
# Football_data.py itself has ENRICH_MAX / ENRICH_SECONDS.
# These limits stop smart_ticket from hammering the API.
FOOTBALL_MAX_STUDIED = 10
FOOTBALL_STUDY_SECONDS = 45

# Minimum football probability before a selection is allowed.
#
# These are intentionally higher than the old 45-55% thresholds.
MIN_FOOTBALL_PROB = {
    "safe": 0.72,
    "normal": 0.68,
    "risky": 0.64,
}

# Evidence quality threshold.
MIN_EVIDENCE_QUALITY = {
    "safe": 0.78,
    "normal": 0.70,
    "risky": 0.64,
}

# Maximum odds for individual selections.
MAX_SELECTION_ODDS = {
    "safe": 2.00,
    "normal": 2.40,
    "risky": 2.80,
}

# Minimum odds.
MIN_ODDS = {
    "up": 1.30,
    "dc": 1.30,
    "over15": 1.30,
    "over": 1.30,
    "btts": 1.60,
    "team_goals": 1.30,
    "streak": 1.30,
    "corners": 1.30,
    "corners_1h": 1.30,
    "handicap": 1.30,
    "asian_handicap": 1.30,
}

# Corners are deliberately capped because Football_data.py
# currently does not have the same depth of corner information
# as its goal model.
MAX_ODDS_KIND = {
    "corners": 1.50,
    "corners_1h": 1.50,
}

# Small diversification preference.
# These DO NOT create probability.
KIND_BONUS = {
    "up": 0.010,
    "team_goals": 0.010,
    "corners": 0.005,
    "corners_1h": 0.005,
    "handicap": 0.005,
    "asian_handicap": 0.005,
    "btts": 0.005,
    "streak": 0.005,
    "dc": 0.002,
    "over15": 0.005,
    "over": 0.005,
}

# Diversification caps.
KIND_CAP = {
    "over15": 0.35,
    "over": 0.30,
    "btts": 0.30,
    "dc": 0.20,
    "up": 0.40,
    "corners": 0.20,
    "corners_1h": 0.15,
    "handicap": 0.25,
    "asian_handicap": 0.25,
    "team_goals": 0.35,
    "streak": 0.20,
}

KIND_NAME = {
    "over15": "Over 1.5",
    "over": "Over goals",
    "btts": "BTTS",
    "dc": "Double chance",
    "up": "1UP / 2UP",
    "corners": "Corners",
    "corners_1h": "1st half corners",
    "handicap": "Handicap",
    "asian_handicap": "Asian handicap",
    "team_goals": "Team goals",
    "streak": "3+ goal streak — No",
}


# ============================================================
# FOOTBALL ENGINE STATUS
# ============================================================

def football_engine_available():
    return fd is not None


def _football_diag():
    if fd is None:
        return ""

    try:
        return str(
            fd.diag_summary()
        )
    except Exception:
        return ""


# ============================================================
# BASIC HELPERS
# ============================================================

def _f(value):
    try:
        return sp._float(value)
    except Exception:
        try:
            return float(value)
        except Exception:
            return None


def _clamp(value, low=0.0, high=0.99):
    try:
        value = float(value)
    except Exception:
        return None

    return max(
        low,
        min(high, value),
    )


def _single(odd):
    """
    Used ONLY as a fallback market-side value.

    This is NOT used as football probability.

    Football probability must come from Football_data.py.
    """
    if not odd:
        return None

    return min(
        0.95 / odd,
        0.97,
    )


def _two_way(odd, other):
    """
    Market-side fallback only.

    Never used as the final football prediction when
    Football_data.py has evidence.
    """
    if not odd:
        return None

    if other:
        try:
            a = 1 / odd
            b = 1 / other
            total = a + b

            if total:
                return a / total
        except Exception:
            pass

    return min(
        0.95 / odd,
        0.97,
    )


def _clean(value):
    return str(
        value or ""
    ).strip()


# ============================================================
# SIDE DETECTION
# ============================================================

def _side_of(outcome, two_way=False):
    label = str(
        outcome.get("desc")
        or outcome.get("name")
        or ""
    ).strip().lower()

    oid = str(
        outcome.get("id")
    )

    if label.startswith("home"):
        return "home"

    if label.startswith("away"):
        return "away"

    if label.startswith("draw"):
        return None

    if oid == "1":
        return "home"

    if oid == "3":
        return "away"

    if two_way and oid == "2":
        return "away"

    return None


# ============================================================
# 1UP / 2UP
# ============================================================

UP_RE = re.compile(
    r"1x2\W+([12])\s*-?\s*up\b",
    re.I,
)

UP_LOOSE_RE = re.compile(
    r"\b([12])\s*-?\s*up\b",
    re.I,
)


def _up_candidates(
    markets,
    home,
    away,
    add,
):
    """
    Collect ONLY SportyBet 1UP / 2UP markets.

    IMPORTANT:
    The probability here is deliberately None.

    Football_data.py must calculate it.
    """

    for market in markets or []:

        text = (
            f"{market.get('desc') or ''} "
            f"{market.get('name') or ''}"
        )

        found = (
            UP_RE.search(text)
            or UP_LOOSE_RE.search(text)
        )

        if not found:
            continue

        outcomes = market.get(
            "outcomes",
            [],
        )

        if len(outcomes) != 3:
            continue

        try:
            up_number = int(
                found.group(1)
            )
        except Exception:
            continue

        if up_number not in (1, 2):
            continue

        spec = (
            market.get(
                "specifier"
            )
            or ""
        )

        for outcome in outcomes:

            if (
                outcome.get(
                    "isActive"
                )
                is False
            ):
                continue

            side = _side_of(
                outcome
            )

            if side not in (
                "home",
                "away",
            ):
                continue

            odd = _f(
                outcome.get(
                    "odds"
                )
            )

            if not odd or odd <= 1:
                continue

            team = (
                home
                if side == "home"
                else away
            )

            add(
                "up",
                (
                    f"{team} to win "
                    f"({up_number}UP)"
                ),
                odd,
                None,
                (
                    str(
                        market.get(
                            "id"
                        )
                    ),
                    spec,
                    str(
                        outcome.get(
                            "id"
                        )
                    ),
                ),
                side=side,
                up_n=up_number,
            )


# ============================================================
# EVENT CANDIDATES
# ============================================================

def event_candidates(
    event,
    markets,
):
    """
    Extract available SportyBet markets.

    This function ONLY answers:

        "What can SportyBet currently offer?"

    It does NOT decide which market is good.

    Football_data.py decides probability later.
    """

    home = event.get(
        "homeTeamName",
        "Home",
    )

    away = event.get(
        "awayTeamName",
        "Away",
    )

    candidates = []

    def add(
        kind,
        label,
        odd,
        probability,
        key,
        **extra,
    ):
        if (
            not odd
            or odd <= 1
        ):
            return

        item = {
            "kind": kind,
            "label": label,
            "odd": odd,
            "market_p": probability,
            "p": None,
            "key": key,
        }

        item.update(extra)

        candidates.append(
            item
        )

    # ========================================================
    # 1X2
    # ========================================================

    h = sp.find_odds(
        markets,
        (
            sp.M_1X2,
            "",
            sp.OUT_1X2["home"],
        ),
    )

    d = sp.find_odds(
        markets,
        (
            sp.M_1X2,
            "",
            sp.OUT_1X2["draw"],
        ),
    )

    a = sp.find_odds(
        markets,
        (
            sp.M_1X2,
            "",
            sp.OUT_1X2["away"],
        ),
    )

    # ========================================================
    # 1UP / 2UP
    # ========================================================

    _up_candidates(
        markets,
        home,
        away,
        add,
    )

    # ========================================================
    # DOUBLE CHANCE 12
    # ========================================================

    odd = sp.find_odds(
        markets,
        (
            sp.M_DC,
            "",
            sp.OUT_DC["12"],
        ),
    )

    if odd:
        add(
            "dc",
            f"{home} or {away}",
            odd,
            None,
            (
                sp.M_DC,
                "",
                sp.OUT_DC["12"],
            ),
        )

    # ========================================================
    # TOTAL GOALS — OVER ONLY
    # ========================================================

    for market in markets or []:

        if (
            str(
                market.get("id")
            )
            != str(sp.M_TOTAL)
        ):
            continue

        spec = (
            market.get(
                "specifier"
            )
            or ""
        )

        if not spec.startswith(
            "total="
        ):
            continue

        line = _f(
            spec.replace(
                "total=",
                "",
            )
        )

        if line not in (
            0.5,
            1.5,
            2.5,
            3.5,
        ):
            continue

        over = None
        under = None

        for outcome in market.get(
            "outcomes",
            [],
        ):

            if (
                outcome.get(
                    "isActive"
                )
                is False
            ):
                continue

            oid = str(
                outcome.get(
                    "id"
                )
            )

            if oid == str(
                sp.OUT_TOTAL["over"]
            ):
                over = _f(
                    outcome.get(
                        "odds"
                    )
                )

            elif oid == str(
                sp.OUT_TOTAL["under"]
            ):
                under = _f(
                    outcome.get(
                        "odds"
                    )
                )

        kind = (
            "over15"
            if line == 1.5
            else "over"
        )

        add(
            kind,
            f"Over {line:g} goals",
            over,
            _two_way(
                over,
                under,
            ),
            (
                str(
                    market.get(
                        "id"
                    )
                ),
                spec,
                sp.OUT_TOTAL["over"],
            ),
            line=line,
        )

    # ========================================================
    # BTTS YES
    # ========================================================

    yes_key = (
        sp.M_BTTS,
        "",
        sp.OUT_BTTS["yes"],
    )

    no_key = (
        sp.M_BTTS,
        "",
        sp.OUT_BTTS["no"],
    )

    yes = sp.find_odds(
        markets,
        yes_key,
    )

    no = sp.find_odds(
        markets,
        no_key,
    )

    add(
        "btts",
        "Both teams to score",
        yes,
        _two_way(
            yes,
            no,
        ),
        yes_key,
    )

    # ========================================================
    # TEAM GOALS
    # ========================================================

    for side, market_id, team in (
        (
            "home",
            sp.M_HOME_TEAM_GOALS,
            home,
        ),
        (
            "away",
            sp.M_AWAY_TEAM_GOALS,
            away,
        ),
    ):

        for market in markets or []:

            if (
                str(
                    market.get("id")
                )
                != str(market_id)
            ):
                continue

            spec = (
                market.get(
                    "specifier"
                )
                or ""
            )

            if not spec.startswith(
                "total="
            ):
                continue

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line not in (
                0.5,
                1.5,
            ):
                continue

            over = None
            under = None

            for outcome in market.get(
                "outcomes",
                [],
            ):

                if (
                    outcome.get(
                        "isActive"
                    )
                    is False
                ):
                    continue

                oid = str(
                    outcome.get(
                        "id"
                    )
                )

                if oid == str(
                    sp.OUT_TEAM_GOALS[
                        "over"
                    ]
                ):
                    over = _f(
                        outcome.get(
                            "odds"
                        )
                    )

                elif oid == str(
                    sp.OUT_TEAM_GOALS[
                        "under"
                    ]
                ):
                    under = _f(
                        outcome.get(
                            "odds"
                        )
                    )

            add(
                "team_goals",
                (
                    f"{team} to score "
                    f"{line:g}+"
                ),
                over,
                _two_way(
                    over,
                    under,
                ),
                (
                    market_id,
                    spec,
                    sp.OUT_TEAM_GOALS[
                        "over"
                    ],
                ),
                side=side,
                line=line,
            )

    # ========================================================
    # 3+ GOAL STREAK — NO
    # ========================================================

    for market in markets or []:

        if (
            str(
                market.get("id")
            )
            != str(sp.M_STREAK_3)
        ):
            continue

        yes = None
        no = None

        for outcome in market.get(
            "outcomes",
            [],
        ):

            if (
                outcome.get(
                    "isActive"
                )
                is False
            ):
                continue

            oid = str(
                outcome.get(
                    "id"
                )
            )

            if oid == str(
                sp.OUT_STREAK["yes"]
            ):
                yes = _f(
                    outcome.get(
                        "odds"
                    )
                )

            elif oid == str(
                sp.OUT_STREAK["no"]
            ):
                no = _f(
                    outcome.get(
                        "odds"
                    )
                )

        if no:

            add(
                "streak",
                (
                    "No team to score "
                    "3+ in a row"
                ),
                no,
                _two_way(
                    no,
                    yes,
                ),
                (
                    sp.M_STREAK_3,
                    "",
                    sp.OUT_STREAK["no"],
                ),
            )

    # ========================================================
    # CORNERS
    # ========================================================

    for market_id, half, lines in (
        (
            sp.M_CORNERS,
            False,
            (
                6.5,
                7.5,
            ),
        ),
        (
            sp.M_CORNERS_1H,
            True,
            (
                3.5,
            ),
        ),
    ):

        for market in markets or []:

            if (
                str(
                    market.get("id")
                )
                != str(market_id)
            ):
                continue

            spec = (
                market.get(
                    "specifier"
                )
                or ""
            )

            if not spec.startswith(
                "total="
            ):
                continue

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line not in lines:
                continue

            over = None
            under = None

            for outcome in market.get(
                "outcomes",
                [],
            ):

                if (
                    outcome.get(
                        "isActive"
                    )
                    is False
                ):
                    continue

                oid = str(
                    outcome.get(
                        "id"
                    )
                )

                if oid == str(
                    sp.OUT_TOTAL["over"]
                ):
                    over = _f(
                        outcome.get(
                            "odds"
                        )
                    )

                elif oid == str(
                    sp.OUT_TOTAL["under"]
                ):
                    under = _f(
                        outcome.get(
                            "odds"
                        )
                    )

            kind = (
                "corners_1h"
                if half
                else "corners"
            )

            prefix = (
                "1st half Over "
                if half
                else "Over "
            )

            add(
                kind,
                (
                    f"{prefix}"
                    f"{line:g} corners"
                ),
                over,
                _two_way(
                    over,
                    under,
                ),
                (
                    market_id,
                    spec,
                    sp.OUT_TOTAL["over"],
                ),
                line=line,
            )

    # ========================================================
    # POSITIVE HANDICAPS ONLY
    # ========================================================

    for market in markets or []:

        market_id = str(
            market.get("id")
        )

        if market_id not in (
            str(sp.M_HANDICAP),
            str(sp.M_ASIAN_HANDICAP),
        ):
            continue

        spec = (
            market.get(
                "specifier"
            )
            or ""
        )

        if not spec.startswith(
            "hcp="
        ):
            continue

        kind = (
            "asian_handicap"
            if market_id
            == str(
                sp.M_ASIAN_HANDICAP
            )
            else "handicap"
        )

        for outcome in market.get(
            "outcomes",
            [],
        ):

            if (
                outcome.get(
                    "isActive"
                )
                is False
            ):
                continue

            side = _side_of(
                outcome
            )

            if side not in (
                "home",
                "away",
            ):
                continue

            odd = _f(
                outcome.get(
                    "odds"
                )
            )

            if not odd:
                continue

            label = str(
                outcome.get(
                    "desc"
                )
                or outcome.get(
                    "name"
                )
                or ""
            )

            if "+" not in label:
                continue

            team = (
                home
                if side == "home"
                else away
            )

            add(
                kind,
                f"{team} {label.strip()}",
                odd,
                _single(odd),
                (
                    market_id,
                    spec,
                    str(
                        outcome.get(
                            "id"
                        )
                    ),
                ),
                side=side,
                handicap_label=label.strip(),
            )

    return candidates


# ============================================================
# DETAILED MARKET CACHE
# ============================================================

_DETAIL_LOCK = threading.Lock()


def _slim_markets(markets):
    keep = []

    wanted_ids = {
        str(sp.M_1X2),
        str(sp.M_DC),
        str(sp.M_TOTAL),
        str(sp.M_BTTS),
        str(sp.M_HOME_TEAM_GOALS),
        str(sp.M_AWAY_TEAM_GOALS),
        str(sp.M_CORNERS),
        str(sp.M_CORNERS_1H),
        str(sp.M_STREAK_3),
        str(sp.M_HANDICAP),
        str(sp.M_ASIAN_HANDICAP),
    }

    for market in markets or []:

        market_id = str(
            market.get("id")
        )

        if market_id in wanted_ids:
            keep.append(
                market
            )
            continue

        text = (
            f"{market.get('desc') or ''} "
            f"{market.get('name') or ''}"
        )

        if UP_LOOSE_RE.search(text):
            keep.append(
                market
            )

    return keep


def _event_markets_cached(
    self,
    event_id,
):
    cache = getattr(
        self,
        "_sportytips_market_cache",
        None,
    )

    if cache is None:
        cache = {}
        self._sportytips_market_cache = cache

    now = time.time()

    cached = cache.get(
        event_id
    )

    if cached:

        timestamp, markets = cached

        if (
            now - timestamp
            < getattr(
                sp,
                "UP_MARKETS_CACHE_SECONDS",
                180,
            )
        ):
            return markets

    markets = _slim_markets(
        self.get_event_markets(
            event_id
        )
    )

    cache[event_id] = (
        now,
        markets,
    )

    if (
        len(cache)
        > MAX_CACHED_MATCHES
    ):

        oldest = sorted(
            cache,
            key=lambda key:
                cache[key][0],
        )

        remove_count = (
            len(cache)
            - MAX_CACHED_MATCHES
        )

        for key in oldest[
            :remove_count
        ]:
            cache.pop(
                key,
                None,
            )

    return markets


sp.SportyBetProvider._event_markets_cached = (
    _event_markets_cached
)


# ============================================================
# FOOTBALL EVIDENCE
# ============================================================

def _get_fixture(event):
    """
    Match the SportyBet event against API-Football.
    """

    if fd is None:
        return None

    try:
        return fd.find_fixture(
            event
        )
    except Exception as exc:
        print(
            "Football fixture matching failed:",
            exc,
        )
        return None


def _get_facts(fixture):
    if fd is None or not fixture:
        return None

    try:
        return fd.get_facts(
            fixture
        )
    except Exception as exc:
        print(
            "Football facts failed:",
            exc,
        )
        return None


def _football_probability(
    candidate,
    facts,
):
    """
    Ask Football_data.py for the actual football probability.

    No bookmaker probability is used.
    """

    if fd is None or facts is None:
        return None

    try:

        probability = fd.adjusted_p(
            candidate,
            facts,
        )

        if probability is None:
            return None

        return _clamp(
            probability,
            0.01,
            0.97,
        )

    except Exception as exc:

        print(
            "Football probability failed:",
            exc,
        )

        return None


def _facts_quality(facts):
    """
    Estimate how complete the football evidence is.

    This is intentionally conservative.
    """

    if not facts:
        return 0.0

    scores = []

    # Recent form.
    home_form = facts.get(
        "home_form"
    )

    away_form = facts.get(
        "away_form"
    )

    if home_form and away_form:
        scores.append(1.0)
    elif home_form or away_form:
        scores.append(0.55)

    # Goal averages.
    required_goal_fields = (
        "home_gf",
        "home_ga",
        "away_gf",
        "away_ga",
    )

    present_goals = sum(
        1
        for key in required_goal_fields
        if facts.get(key) is not None
    )

    if present_goals == 4:
        scores.append(1.0)
    elif present_goals >= 2:
        scores.append(0.65)

    # Venue.
    venue_fields = (
        "home_home",
        "away_away",
    )

    venue_present = sum(
        1
        for key in venue_fields
        if facts.get(key)
    )

    if venue_present == 2:
        scores.append(1.0)
    elif venue_present:
        scores.append(0.60)

    # H2H.
    if facts.get(
        "h2h_count"
    ):
        scores.append(0.90)

    # API model.
    api_model = facts.get(
        "api_model"
    )

    if api_model:
        scores.append(1.0)

    if not scores:
        return 0.0

    return _clamp(
        sum(scores) / len(scores),
        0.0,
        1.0,
    )


def _model_disagreement_penalty(
    candidate,
    facts,
    football_p,
):
    """
    Detect situations where the football model is weak or
    internally uncertain.

    We deliberately DO NOT compare against SportyBet odds.

    The bookmaker must not be allowed to validate the model.
    """

    if not facts:
        return 0.0

    api_model = facts.get(
        "api_model"
    )

    if not api_model:
        return 0.0

    kind = candidate.get(
        "kind"
    )

    side = candidate.get(
        "side"
    )

    api_p = None

    try:

        if kind in (
            "up",
            "dc",
            "handicap",
            "asian_handicap",
        ):

            if side == "home":
                api_p = (
                    api_model.get(
                        "home"
                    )
                )

            elif side == "away":
                api_p = (
                    api_model.get(
                        "away"
                    )
                )

        elif kind == "btts":

            api_p = (
                api_model.get(
                    "btts"
                )
            )

        elif kind in (
            "over",
            "over15",
        ):

            line = candidate.get(
                "line"
            )

            key = (
                f"over_{line:g}"
                if line is not None
                else "over"
            )

            api_p = api_model.get(
                key
            )

    except Exception:
        api_p = None

    if api_p is None:
        return 0.0

    try:
        disagreement = abs(
            float(
                football_p
            )
            - float(
                api_p
            )
        )
    except Exception:
        return 0.0

    # Only penalise meaningful disagreement.
    if disagreement <= 0.08:
        return 0.0

    if disagreement <= 0.15:
        return 0.025

    if disagreement <= 0.22:
        return 0.05

    return 0.08


def _apply_evidence_score(
    candidate,
    facts,
    risk,
):
    """
    Convert Football_data probability into the final internal
    selection probability.

    This is still football-only.

    Evidence quality and disagreement can LOWER confidence.
    They can NEVER increase probability.
    """

    raw_p = _football_probability(
        candidate,
        facts,
    )

    if raw_p is None:
        return None

    quality = _facts_quality(
        facts
    )

    disagreement = (
        _model_disagreement_penalty(
            candidate,
            facts,
            raw_p,
        )
    )

    # Incomplete data gets penalised.
    missing_penalty = max(
        0.0,
        (0.78 - quality)
        * 0.12,
    )

    final_p = (
        raw_p
        - missing_penalty
        - disagreement
    )

    final_p = _clamp(
        final_p,
        0.01,
        0.97,
    )

    candidate[
        "football_p"
    ] = raw_p

    candidate[
        "evidence_quality"
    ] = quality

    candidate[
        "model_disagreement"
    ] = disagreement

    candidate[
        "p"
    ] = final_p

    return final_p


# ============================================================
# EVIDENCE REASON
# ============================================================

def _football_reason(
    candidate,
    facts,
):
    """
    Prefer Football_data.py's reason_for().

    If unavailable, create a compact evidence summary.
    """

    if fd is not None:

        try:

            reason = fd.reason_for(
                candidate,
                facts,
            )

            if reason:
                return str(
                    reason
                )

        except Exception:
            pass

    if not facts:
        return (
            "Football evidence unavailable."
        )

    parts = []

    try:

        home_form = facts.get(
            "home_form"
        )

        away_form = facts.get(
            "away_form"
        )

        if home_form:
            parts.append(
                f"{candidate.get('home')} form "
                f"{home_form}"
            )

        if away_form:
            parts.append(
                f"{candidate.get('away')} form "
                f"{away_form}"
            )

        hgf = facts.get(
            "home_gf"
        )

        hga = facts.get(
            "home_ga"
        )

        agf = facts.get(
            "away_gf"
        )

        aga = facts.get(
            "away_ga"
        )

        if all(
            value is not None
            for value in (
                hgf,
                hga,
                agf,
                aga,
            )
        ):
            parts.append(
                "recent goal profile supports "
                "the selection"
            )

    except Exception:
        pass

    if not parts:
        return (
            "Football evidence supports "
            "the selection."
        )

    return (
        "; ".join(parts)
        + "."
    )


def _reason_for(candidate):
    """
    Final human-readable reason.

    Football evidence is preferred.
    """

    evidence_reason = candidate.get(
        "football_reason"
    )

    probability = candidate.get(
        "p"
    )

    quality = candidate.get(
        "evidence_quality"
    )

    if evidence_reason:

        suffix = ""

        if probability is not None:
            suffix += (
                f" Model confidence "
                f"{round(probability * 100)}%."
            )

        if quality is not None:
            suffix += (
                f" Evidence quality "
                f"{round(quality * 100)}%."
            )

        return (
            str(evidence_reason)
            + suffix
        )

    return (
        f"Football model supports "
        f"this selection at "
        f"{round((probability or 0) * 100)}%."
    )


# ============================================================
# CANDIDATE ENRICHMENT
# ============================================================

def _enrich_candidate(
    candidate,
    event,
    facts,
):
    probability = _apply_evidence_score(
        candidate,
        facts,
        "normal",
    )

    if probability is None:
        return False

    candidate[
        "football_reason"
    ] = _football_reason(
        candidate,
        facts,
    )

    candidate[
        "reason"
    ] = _reason_for(
        candidate
    )

    candidate[
        "football_data"
    ] = facts

    return True


# ============================================================
# DEDUPLICATION
# ============================================================

def _candidate_identity(candidate):
    return (
        candidate.get(
            "event_id"
        ),
        candidate.get(
            "key"
        ),
    )


# ============================================================
# GATHER
# ============================================================

def gather(
    provider,
    start,
    end,
    risk,
    floor,
    exclude,
    min_p_shift=0.0,
    notify=None,
    straight_only=False,
    max_groups=GROUP_LIMIT,
):
    """
    Main evidence-first selection engine.
    """

    started = time.time()

    if fd is None:

        print(
            "Football_data.py unavailable. "
            "Evidence-first mode cannot continue."
        )

        return (
            [],
            0,
            0,
            0,
        )

    events = provider.get_upcoming(
        start,
        end,
    )

    if not events:

        print(
            "SportyBet returned 0 events."
        )

        return (
            [],
            0,
            0,
            0,
        )

    # --------------------------------------------------------
    # DO NOT RANK MATCHES BY ODDS.
    #
    # We only prioritize events by whether they are likely to
    # be enrichable. This is NOT a betting probability.
    # --------------------------------------------------------

    wanted = []

    for event in events:

        if not event.get(
            "eventId"
        ):
            continue

        wanted.append(
            event
        )

        limit = (
            STRAIGHT_DETAIL_EVENTS
            if straight_only
            else MAX_DETAIL_EVENTS
        )

        if len(wanted) >= limit:
            break

    # --------------------------------------------------------
    # SPORTYBET MARKET LOADING
    # --------------------------------------------------------

    details = {}

    if wanted:

        pool = ThreadPoolExecutor(
            max_workers=DETAIL_WORKERS
        )

        futures = {
            pool.submit(
                provider._event_markets_cached,
                event["eventId"],
            ): event
            for event in wanted
        }

        try:

            for future in as_completed(
                futures,
                timeout=DETAIL_SECONDS,
            ):

                event = futures[
                    future
                ]

                event_id = event[
                    "eventId"
                ]

                try:

                    details[
                        event_id
                    ] = future.result()

                except Exception as exc:

                    print(
                        "Market request failed:",
                        exc,
                    )

        except FutureTimeout:

            print(
                "Detailed market loading timed out."
            )

        finally:

            pool.shutdown(
                wait=False,
                cancel_futures=True,
            )

    # --------------------------------------------------------
    # MINIMUM PROBABILITY
    # --------------------------------------------------------

    min_p = (
        MIN_FOOTBALL_PROB.get(
            risk,
            MIN_FOOTBALL_PROB[
                "normal"
            ],
        )
        - min_p_shift
    )

    quality_floor = (
        MIN_EVIDENCE_QUALITY.get(
            risk,
            MIN_EVIDENCE_QUALITY[
                "normal"
            ],
        )
    )

    max_odd = (
        MAX_SELECTION_ODDS.get(
            risk,
            MAX_SELECTION_ODDS[
                "normal"
            ],
        )
    )

    # Straight win is allowed to search slightly wider,
    # but it still cannot bypass the football model.
    if straight_only:

        min_p = max(
            0.62,
            min_p - 0.02,
        )

        max_odd = max(
            max_odd,
            2.60,
        )

    groups = []

    # ========================================================
    # FOOTBALL EVIDENCE PASS
    # ========================================================

    football_studied = 0
    football_started = time.time()

    for event in wanted:

        if (
            football_studied
            >= FOOTBALL_MAX_STUDIED
        ):
            break

        if (
            time.time()
            - football_started
            >= FOOTBALL_STUDY_SECONDS
        ):
            break

        event_id = event.get(
            "eventId"
        )

        markets = (
            details.get(
                event_id
            )
            or event.get(
                "markets"
            )
            or []
        )

        if not markets:
            continue

        # ----------------------------------------------------
        # Match SportyBet event to API-Football.
        # ----------------------------------------------------

        fixture = _get_fixture(
            event
        )

        if not fixture:
            continue

        facts = _get_facts(
            fixture
        )

        if not facts:
            continue

        football_studied += 1

        candidates = event_candidates(
            event,
            markets,
        )

        kept = []
        labels = set()

        # SportyBet fixture metadata.
        sporty_fixture = (
            sp.sporty_fixture(
                event
            )
        )

        league_name = (
            sporty_fixture
            .get(
                "league",
                {},
            )
            .get(
                "name",
                "Football",
            )
        )

        # ----------------------------------------------------
        # Calculate football probability for every candidate.
        # ----------------------------------------------------

        for candidate in candidates:

            # Straight Win = ONLY 1UP / 2UP.
            if straight_only:

                if candidate.get(
                    "kind"
                ) != "up":
                    continue

                if candidate.get(
                    "up_n"
                ) not in (
                    1,
                    2,
                ):
                    continue

            kind = candidate[
                "kind"
            ]

            odd = candidate[
                "odd"
            ]

            # Minimum SportyBet price.
            floor_kind = MIN_ODDS.get(
                kind,
                1.30,
            )

            if odd < floor_kind:
                continue

            # Maximum price for corners.
            cap_kind = MAX_ODDS_KIND.get(
                kind
            )

            if (
                cap_kind
                and odd > cap_kind
            ):
                continue

            # Global price safety.
            if odd > max_odd:
                continue

            # Excluded selection.
            if (
                event_id,
                candidate[
                    "key"
                ],
            ) in exclude:
                continue

            # ------------------------------------------------
            # FOOTBALL MODEL
            # ------------------------------------------------

            probability = _apply_evidence_score(
                candidate,
                facts,
                risk,
            )

            if probability is None:
                continue

            quality = candidate.get(
                "evidence_quality",
                0.0,
            )

            # Evidence quality gate.
            if quality < quality_floor:
                continue

            # Football probability gate.
            if probability < min_p:
                continue

            # Duplicate labels.
            if candidate[
                "label"
            ] in labels:
                continue

            labels.add(
                candidate[
                    "label"
                ]
            )

            # ------------------------------------------------
            # MATCH METADATA
            # ------------------------------------------------

            timestamp = (
                event.get(
                    "estimateStartTime"
                )
                or 0
            )

            try:

                kickoff = (
                    datetime.fromtimestamp(
                        timestamp / 1000,
                        tz=timezone.utc,
                    )
                )

            except Exception:

                kickoff = (
                    datetime.now(
                        timezone.utc
                    )
                )

            candidate.update(
                {
                    "event": event,
                    "event_id": event_id,
                    "home": event.get(
                        "homeTeamName",
                        "Home",
                    ),
                    "away": event.get(
                        "awayTeamName",
                        "Away",
                    ),
                    "kickoff": kickoff,
                    "league": league_name,
                    "fixture": fixture,
                    "facts": facts,
                }
            )

            candidate[
                "football_reason"
            ] = _football_reason(
                candidate,
                facts,
            )

            candidate[
                "reason"
            ] = _reason_for(
                candidate
            )

            kept.append(
                candidate
            )

        # ----------------------------------------------------
        # Keep only the strongest football-supported markets
        # from each match.
        # ----------------------------------------------------

        if kept:

            kept.sort(
                key=lambda candidate:
                    (
                        candidate.get(
                            "p",
                            0,
                        )
                        + KIND_BONUS.get(
                            candidate.get(
                                "kind"
                            ),
                            0,
                        )
                    ),
                reverse=True,
            )

            groups.append(
                kept[
                    :MAX_OPTIONS_PER_MATCH
                ]
            )

    # ========================================================
    # GLOBAL SORT
    # ========================================================

    groups.sort(
        key=lambda group:
            max(
                candidate.get(
                    "p",
                    0,
                )
                for candidate in group
            ),
        reverse=True,
    )

    groups = groups[
        :max_groups
    ]

    elapsed = (
        time.time()
        - started
    )

    print(
        "evidence-first gather:",
        len(events),
        "SportyBet matches,",
        len(details),
        "market details,",
        football_studied,
        "football matches studied,",
        len(groups),
        "usable groups,",
        f"{elapsed:.1f}s",
    )

    return (
        groups,
        len(events),
        len(details),
        football_studied,
    )


# ============================================================
# ODDS PRODUCT
# ============================================================

def _product(picks):
    total = 1.0

    for pick in picks:
        total *= pick[
            "odd"
        ]

    return total


# ============================================================
# DP ENGINE
# ============================================================

def _dp(
    groups,
    target,
):
    """
    Find a combination reaching target odds.

    IMPORTANT:
    The DP uses football probability as its cost.

    SportyBet odds only determine whether the target can
    physically be reached.
    """

    SCALE = 80

    if target <= 1:
        return None

    target_weight = math.ceil(
        math.log(
            target
        )
        * SCALE
    )

    max_weight = max(
        target_weight,
        int(
            math.log(
                target
                * (
                    1
                    + OVERSHOOT
                )
            )
            * SCALE
        ),
    )

    INF = float("inf")

    dp = [
        INF
        for _ in range(
            max_weight + 1
        )
    ]

    dp[0] = 0.0

    choices = []

    for group in groups:

        new = dp[:]

        choice = [
            None
            for _ in range(
                max_weight + 1
            )
        ]

        for index, candidate in enumerate(
            group
        ):

            odd = candidate.get(
                "odd"
            )

            probability = candidate.get(
                "p"
            )

            if (
                not odd
                or odd <= 1
                or not probability
                or probability <= 0
            ):
                continue

            weight = round(
                math.log(
                    odd
                )
                * SCALE
            )

            if (
                weight <= 0
                or weight > max_weight
            ):
                continue

            # ------------------------------------------------
            # FOOTBALL MODEL COST
            #
            # Higher football probability = lower cost.
            #
            # This is the opposite of the old odds-driven
            # engine.
            # ------------------------------------------------

            cost = (
                -math.log(
                    probability
                )
                - (
                    KIND_BONUS.get(
                        candidate.get(
                            "kind"
                        ),
                        0,
                    )
                    * 0.10
                )
            )

            for x in range(
                max_weight
                - weight
                + 1
            ):

                if dp[x] == INF:
                    continue

                value = (
                    dp[x]
                    + cost
                )

                destination = (
                    x + weight
                )

                if (
                    value
                    < new[
                        destination
                    ]
                ):

                    new[
                        destination
                    ] = value

                    choice[
                        destination
                    ] = (
                        index,
                        x,
                    )

        dp = new

        choices.append(
            choice
        )

    best = None

    for weight in range(
        target_weight,
        max_weight + 1,
    ):

        if dp[
            weight
        ] == INF:
            continue

        if (
            best is None
            or dp[
                weight
            ]
            < dp[
                best
            ]
        ):
            best = weight

    if best is None:
        return None

    picked = []

    x = best

    for group_index in range(
        len(groups) - 1,
        -1,
        -1,
    ):

        step = choices[
            group_index
        ][x]

        if step is None:
            continue

        candidate_index, previous_x = (
            step
        )

        picked.append(
            groups[
                group_index
            ][
                candidate_index
            ]
        )

        x = previous_x

    picked.reverse()

    # --------------------------------------------------------
    # Safety:
    # One match = one selection.
    # --------------------------------------------------------

    used = set()
    final = []

    for pick in picked:

        event_id = pick.get(
            "event_id"
        )

        if event_id in used:
            continue

        used.add(
            event_id
        )

        final.append(
            pick
        )

    return final


def _dp_exact(
    groups,
    target,
):
    best = None

    aim = target

    for _ in range(4):

        picks = _dp(
            groups,
            aim,
        )

        if not picks:
            break

        actual = _product(
            picks
        )

        if (
            actual >= target
            and (
                best is None
                or actual
                < _product(
                    best
                )
            )
        ):

            best = picks

        if (
            target
            <= actual
            <= target * 1.10
        ):

            return picks

        if actual <= 1:
            break

        aim = max(
            target,
            aim
            * target
            / actual
            * 1.01,
        )

    return best


# ============================================================
# KIND COUNTS
# ============================================================

def _kind_counts(picks):
    counts = {}

    for pick in picks:

        kind = pick[
            "kind"
        ]

        counts[kind] = (
            counts.get(
                kind,
                0,
            )
            + 1
        )

    return counts


# ============================================================
# CHOOSE TARGET
# ============================================================

def choose_target(
    groups,
    target,
    caps=True,
):
    if not groups:
        return [], False

    picks = _dp_exact(
        groups,
        target,
    )

    if picks:
        return (
            picks,
            True,
        )

    # --------------------------------------------------------
    # Target cannot be reached.
    #
    # Do NOT force weak selections.
    #
    # Return the strongest football-supported picks instead.
    # --------------------------------------------------------

    ranked = []

    for group in groups:

        best = max(
            group,
            key=lambda candidate:
                (
                    candidate.get(
                        "p",
                        0,
                    )
                    + KIND_BONUS.get(
                        candidate.get(
                            "kind"
                        ),
                        0,
                    )
                ),
        )

        ranked.append(
            best
        )

    ranked.sort(
        key=lambda candidate:
            candidate.get(
                "p",
                0,
            ),
        reverse=True,
    )

    result = []
    used = set()

    for candidate in ranked:

        event_id = candidate[
            "event_id"
        ]

        if event_id in used:
            continue

        result.append(
            candidate
        )

        used.add(
            event_id
        )

        if len(result) >= MAX_LEGS:
            break

    actual = _product(
        result
    )

    return (
        result,
        actual >= target,
    )


# ============================================================
# CHOOSE COUNT
# ============================================================

def choose_count(
    groups,
    count,
    caps=True,
):
    candidates = sorted(
        (
            candidate
            for group in groups
            for candidate in group
        ),
        key=lambda candidate:
            (
                candidate.get(
                    "p",
                    0,
                )
                + KIND_BONUS.get(
                    candidate.get(
                        "kind"
                    ),
                    0,
                )
            ),
        reverse=True,
    )

    chosen = []
    used_events = set()
    counts = {}

    for candidate in candidates:

        event_id = candidate[
            "event_id"
        ]

        if event_id in used_events:
            continue

        kind = candidate[
            "kind"
        ]

        if caps:

            cap = max(
                1,
                math.ceil(
                    KIND_CAP.get(
                        kind,
                        0.30,
                    )
                    * count
                    * 1.5
                ),
            )

            if (
                counts.get(
                    kind,
                    0,
                )
                >= cap
            ):
                continue

        chosen.append(
            candidate
        )

        used_events.add(
            event_id
        )

        counts[kind] = (
            counts.get(
                kind,
                0,
            )
            + 1
        )

        if len(chosen) >= count:
            return (
                chosen,
                True,
            )

    return (
        chosen,
        len(chosen) >= count,
    )


# ============================================================
# DISPLAY HELPERS
# ============================================================

def _time_text(
    candidate,
    today,
):
    local = candidate[
        "kickoff"
    ].astimezone(
        bot.LOCAL_TZ
    )

    text = local.strftime(
        "%I:%M %p"
    ).lstrip("0")

    if local.date() == today:
        return text

    return (
        local.strftime("%a ")
        + text
    )


def _confidence(probability):
    """
    More honest confidence labels.

    85+ = Elite
    80-84 = Very strong
    75-79 = Strong
    70-74 = Acceptable
    65-69 = Weak
    below 65 = Reject
    """

    if probability >= 0.85:
        return "🟢 Elite"

    if probability >= 0.80:
        return "🟢 Very strong"

    if probability >= 0.75:
        return "🟡 Strong"

    if probability >= 0.70:
        return "🟡 Acceptable"

    if probability >= 0.65:
        return "🟠 Weak"

    return "🔴 Reject"


def _fmt(value):

    if value == int(value):
        return str(
            int(value)
        )

    return f"{value:g}"


# ============================================================
# BUILD TICKET
# ============================================================

def build_ticket(
    provider,
    req,
    target,
    count,
    risk,
    exclude=frozenset(),
    notify=None,
    straight_only=False,
    max_days=None,
):
    if target:

        floor = max(
            1.10,
            min(
                1.45,
                target
                ** (
                    1 / 30
                ),
            ),
        )

    else:

        floor = 1.15

    if risk == "risky":

        floor = max(
            floor,
            1.30,
        )

    if target:

        max_groups = max(
            30,
            min(
                GROUP_LIMIT,
                int(
                    math.log(
                        target
                    )
                    * 16
                ),
            ),
        )

    else:

        max_groups = max(
            30,
            min(
                GROUP_LIMIT,
                (count or 5)
                * 5,
            ),
        )

    extra_days = 0

    hard_limit = (
        max_days
        if max_days is not None
        else 3
    )

    chosen = []
    reached = False
    note = ""

    while True:

        end = (
            req["end"]
            + timedelta(
                days=extra_days
            )
        )

        (
            groups,
            total_events,
            detailed,
            studied,
        ) = gather(
            provider,
            req["start"],
            end,
            risk,
            floor,
            exclude,
            0.02
            if target
            and target <= 20
            else 0.0,
            notify,
            straight_only=straight_only,
            max_groups=max_groups,
        )

        if target:

            chosen, reached = (
                choose_target(
                    groups,
                    target,
                    caps=not straight_only,
                )
            )

        else:

            chosen, reached = (
                choose_count(
                    groups,
                    count or 5,
                    caps=not straight_only,
                )
            )

        if reached:
            break

        if extra_days >= hard_limit:
            break

        extra_days += 1

    if extra_days and chosen:

        note = (
            "I extended the search "
            f"by {extra_days} day"
            f"{'s' if extra_days != 1 else ''}."
        )

    return {
        "chosen": chosen,
        "reached": reached,
        "note": note,
        "events": total_events,
        "detailed": detailed,
        "studied": studied,
        "floor": floor,
        "days_used": (
            extra_days + 1
        ),
    }


# ============================================================
# RESET STRAIGHT MODE
# ============================================================

def _reset_straight():

    try:

        import upgrades

        upgrades.STRAIGHT_WIN_ONLY = False

    except Exception:
        pass


# ============================================================
# MAIN FLOW
# ============================================================

def flow(
    chat_id,
    text,
    search_days=None,
    target_override=None,
    count_override=None,
):
    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    if provider is None:

        return _orig_flow(
            chat_id,
            text,
        )

    if fd is None:

        bot.send_message(
            chat_id,
            (
                "❌ The football evidence engine "
                "is unavailable right now. "
                "I won't generate a prediction "
                "from SportyBet odds alone."
            ),
        )

        return

    try:

        req = bot.parse_request(
            text
        )

    except Exception:

        return _orig_flow(
            chat_id,
            text,
        )

    text = str(
        text or ""
    )

    # ========================================================
    # CUSTOM BUILDER OVERRIDES
    # ========================================================

    if target_override is not None:

        try:

            target_override = float(
                target_override
            )

            if target_override > 0:

                req[
                    "target_odds"
                ] = target_override

        except (
            TypeError,
            ValueError,
        ):
            pass

    if count_override is not None:

        try:

            count_override = int(
                count_override
            )

            if count_override > 0:

                req[
                    "picks"
                ] = count_override

        except (
            TypeError,
            ValueError,
        ):
            pass

    # ========================================================
    # SEARCH WINDOW
    # ========================================================

    if search_days is not None:

        try:

            search_days = int(
                search_days
            )

        except (
            TypeError,
            ValueError,
        ):

            search_days = None

        if search_days not in (
            1,
            2,
            3,
            5,
            7,
            14,
        ):

            search_days = None

    if search_days is not None:

        if search_days == 1:

            local_now = datetime.now(
                timezone.utc
            ).astimezone(
                bot.LOCAL_TZ
            )

            tomorrow_midnight = (
                local_now.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                + timedelta(
                    days=1
                )
            )

            req[
                "end"
            ] = (
                tomorrow_midnight.astimezone(
                    timezone.utc
                )
            )

        else:

            req[
                "end"
            ] = (
                req["start"]
                + timedelta(
                    days=search_days - 1
                )
            )

    # ========================================================
    # FINAL TARGET / COUNT
    # ========================================================

    target = req.get(
        "target_odds"
    )

    count = req.get(
        "picks"
    )

    risk = (
        req.get(
            "risk"
        )
        or "normal"
    )

    if not target and not count:
        count = 5

    # ========================================================
    # STRAIGHT WIN
    # ========================================================

    straight_only = bool(
        re.search(
            r"\bstraight\s*-?\s*win(?:s)?\b",
            text,
            re.I,
        )
    )

    if not straight_only:

        has_1up = bool(
            re.search(
                r"\b1\s*-?\s*up\b",
                text,
                re.I,
            )
        )

        has_2up = bool(
            re.search(
                r"\b2\s*-?\s*up\b",
                text,
                re.I,
            )
        )

        if has_1up and has_2up:
            straight_only = True

    straight_today = (
        straight_only
        and bool(
            re.search(
                r"\btoday\b",
                text,
                re.I,
            )
        )
        and not bool(
            re.search(
                r"\blong\b",
                text,
                re.I,
            )
        )
    )

    # ========================================================
    # INTRO
    # ========================================================

    if straight_only:

        intro = (
            "🧠 Checking football evidence "
            "for SportyBet 1UP / 2UP..."
        )

    elif target:

        intro = (
            "🧠 Analysing football evidence "
            f"before checking "
            f"{_fmt(target)} odds..."
        )

    else:

        intro = (
            "🧠 Analysing football evidence "
            f"for the best {count} picks..."
        )

    bot.send_message(
        chat_id,
        intro,
    )

    started = time.time()

    if straight_only:

        try:

            import upgrades

            upgrades.STRAIGHT_WIN_ONLY = True

        except Exception:
            pass

    # ========================================================
    # BUILD
    # ========================================================

    try:

        built = build_ticket(
            provider,
            req,
            target,
            count,
            risk,
            straight_only=straight_only,
            max_days=(
                0
                if (
                    straight_today
                    or search_days is not None
                )
                else None
            ),
        )

    except Exception as exc:

        traceback.print_exc()

        _reset_straight()

        bot.send_message(
            chat_id,
            (
                "❌ The prediction engine "
                "could not complete the analysis.\n"
                + html.escape(
                    str(exc)[:200]
                )
            ),
        )

        return

    if search_days is not None:
        built[
            "days_used"
        ] = search_days

    elapsed = (
        time.time()
        - started
    )

    chosen = built[
        "chosen"
    ]

    print(
        f"Evidence-first ticket built "
        f"in {elapsed:.1f}s: "
        f"{len(chosen)} picks, "
        f"target={target}, "
        f"straight={straight_only}, "
        f"search_days={search_days}"
    )

    # ========================================================
    # NO PICKS
    # ========================================================

    if not chosen:

        bot.send_message(
            chat_id,
            (
                "❌ I couldn't find enough "
                "football-supported selections "
                "in that window.\n\n"
                "I rejected the weak matches "
                "instead of using SportyBet odds "
                "to manufacture confidence."
            ),
        )

        _reset_straight()

        return

    # ========================================================
    # FINAL STRAIGHT WIN SAFETY
    # ========================================================

    if straight_only:

        chosen = [
            candidate
            for candidate in chosen
            if (
                candidate.get(
                    "kind"
                )
                == "up"
                and candidate.get(
                    "up_n"
                )
                in (
                    1,
                    2,
                )
            )
        ]

        if not chosen:

            bot.send_message(
                chat_id,
                (
                    "❌ No valid football-supported "
                    "1UP / 2UP selections were found."
                ),
            )

            _reset_straight()

            return

    # ========================================================
    # TODAY STRAIGHT TARGET
    # ========================================================

    if (
        straight_only
        and straight_today
        and target
        and not built[
            "reached"
        ]
    ):

        actual = _product(
            chosen
        )

        bot.send_message(
            chat_id,
            (
                f"❌ Today only reaches about "
                f"{actual:.1f} odds.\n"
                f"Target: {_fmt(target)}.\n\n"
                "I won't add weak 1UP / 2UP picks "
                "just to force the target."
            ),
        )

        _reset_straight()

        return

    # ========================================================
    # FINAL SORT
    # ========================================================

    chosen.sort(
        key=lambda candidate:
            candidate[
                "kickoff"
            ]
    )

    # ========================================================
    # FINAL PROBABILITY
    # ========================================================

    total_odds = 1.0
    chance = 1.0

    for candidate in chosen:

        total_odds *= candidate[
            "odd"
        ]

        probability = candidate.get(
            "p"
        )

        if probability is None:
            probability = 0.0

        chance *= probability

        candidate[
            "reason"
        ] = _reason_for(
            candidate
        )

    # ========================================================
    # REAL SPORTYBET BOOKING CODE
    # ========================================================

    code = None
    errors = []

    try:

        code = (
            provider.create_booking_code(
                [
                    (
                        candidate[
                            "event"
                        ],
                        {
                            "resolved_key":
                                candidate[
                                    "key"
                                ]
                        },
                    )
                    for candidate in chosen
                ]
            )
        )

    except Exception as exc:

        print(
            "Booking code failed:",
            exc,
        )

        errors.append(
            "Booking code failed: "
            + str(exc)
        )

    # ========================================================
    # RESPONSE
    # ========================================================

    local_now = (
        datetime.now(
            timezone.utc
        ).astimezone(
            bot.LOCAL_TZ
        )
    )

    today = local_now.date()

    if target:

        title = (
            f"{_fmt(target)} ODDS"
        )

    else:

        title = (
            f"{len(chosen)} PICKS"
        )

    if straight_only:

        title = (
            "STRAIGHT WIN · "
            + title
        )

    lines = [
        (
            f"🎯 <b>{BRAND} — "
            f"{html.escape(title)}</b>"
        ),
        (
            f"📅 "
            f"{html.escape(req['label'].capitalize())}"
            f" • {bot.LOCAL_TZ_NAME}"
        ),
        "",
        "🧠 <b>Football evidence first</b>",
    ]

    # ========================================================
    # PICKS
    # ========================================================

    for number, candidate in enumerate(
        chosen,
        start=1,
    ):

        lines.append("")

        time_text = html.escape(
            _time_text(
                candidate,
                today,
            )
        )

        league_text = html.escape(
            candidate[
                "league"
            ]
        )

        home_text = html.escape(
            candidate[
                "home"
            ]
        )

        away_text = html.escape(
            candidate[
                "away"
            ]
        )

        label_text = html.escape(
            candidate[
                "label"
            ]
        )

        probability = (
            candidate.get(
                "p",
                0,
            )
        )

        quality = (
            candidate.get(
                "evidence_quality",
                0,
            )
        )

        confidence = _confidence(
            probability
        )

        lines.append(
            f"<b>{number}.</b> "
            f"🕒 {time_text}"
            f" • 🏆 {league_text}"
        )

        lines.append(
            f"⚽ "
            f"{home_text}"
            f" vs "
            f"{away_text}"
        )

        lines.append(
            f"✅ <b>"
            f"{label_text}"
            f"</b> • 💰 "
            f"{candidate['odd']:.2f}"
        )

        lines.append(
            f"🧠 {confidence} "
            f"• {round(probability * 100)}%"
        )

        lines.append(
            f"📚 Evidence quality: "
            f"{round(quality * 100)}%"
        )

        lines.append(
            "💬 "
            + html.escape(
                candidate[
                    "reason"
                ]
            )
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━",
            (
                f"💰 <b>Total odds: "
                f"{total_odds:.2f}</b>"
            ),
            (
                f"📊 Estimated combined "
                f"football probability: "
                f"<b>"
                f"{chance * 100:.2f}%"
                f"</b>"
            ),
        ]
    )

    if (
        target
        and not built[
            "reached"
        ]
    ):

        lines.append(
            f"⚠️ I could not safely reach "
            f"{_fmt(target)} odds."
        )

    if built[
        "note"
    ]:

        lines.append(
            "ℹ️ "
            + html.escape(
                built[
                    "note"
                ]
            )
        )

    mix = _kind_counts(
        chosen
    )

    lines.append(
        "🧩 Mix: "
        + ", ".join(
            f"{number} "
            f"{KIND_NAME.get(
                kind,
                kind,
            ).lower()}"
            for kind, number
            in sorted(
                mix.items(),
                key=lambda item:
                    -item[1],
            )
        )
    )

    if code:

        lines.append(
            f"📲 SportyBet code: "
            f"<b>"
            f"{html.escape(
                str(code)
            )}"
            f"</b>"
        )

    lines.append(
        f"🔎 Checked "
        f"{built['events']} SportyBet "
        f"matches across "
        f"{built['days_used']} day(s)."
    )

    lines.append(
        f"🧠 Football matches actually "
        f"studied: {built.get('studied', 0)}"
    )

    if (
        len(chosen) >= 12
        and target
        and target >= 50
    ):

        lines.append(
            "⚠️ Big odds are difficult "
            "to land. Stake small."
        )

    lines.append(
        "⚠️ Predictions are estimates, "
        "not guarantees. 18+."
    )

    for error in errors:

        lines.append(
            "⚠️ "
            + html.escape(
                error[:160]
            )
        )

    bot.send_message(
        chat_id,
        "\n".join(lines),
    )

    _reset_straight()


# ============================================================
# REPLACE MAIN TICKET FLOW
# ============================================================

_orig_flow = (
    bot.prediction_ticket_flow
)

bot.prediction_ticket_flow = flow


# ============================================================
# SEARCH WINDOW
# ============================================================

bot.MAX_DAYS_AHEAD = max(
    getattr(
        bot,
        "MAX_DAYS_AHEAD",
        2,
    ),
    3,
)


# ============================================================
# BACKGROUND SPORTYBET WARMER
# ============================================================

_WARM_LOCK = threading.Lock()


def _start_warmer():

    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    if provider is None:

        print(
            "SportyBet provider unavailable; "
            "warmer not started."
        )

        return

    def refresh():

        if not _WARM_LOCK.acquire(
            blocking=False
        ):
            return

        try:

            print(
                "SportyBet background "
                "refresh starting..."
            )

            provider._events_time = 0

            provider._load_events()

            print(
                "SportyBet background "
                "refresh complete."
            )

        except Exception as exc:

            print(
                "SportyBet background "
                "refresh failed:",
                exc,
            )

        finally:

            _WARM_LOCK.release()

    def loop():

        threading.Thread(
            target=refresh,
            daemon=True,
        ).start()

        while True:

            time.sleep(
                300
            )

            threading.Thread(
                target=refresh,
                daemon=True,
            ).start()

    threading.Thread(
        target=loop,
        daemon=True,
        name="sportytips-warmer",
    ).start()


_start_warmer()