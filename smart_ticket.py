import html
import importlib
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
import json

import main as bot
import sportybet_provider as sp


# ============================================================
# AI CONFIG
# ============================================================

ANTHROPIC_API_KEY = (
    getattr(bot, "ANTHROPIC_API_KEY", None)
    or os.getenv("ANTHROPIC_API_KEY")
)

AI_MODEL = (
    getattr(bot, "AI_MODEL", None)
    or os.getenv("AI_MODEL", "claude-sonnet-4-6")
)


def _parse_ai_json(text):
    parse = getattr(bot, "parse_ai_json", None)

    if parse:
        return parse(text)

    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return None


# ============================================================
# GENERAL SETTINGS
# ============================================================

MIN_LEG_PROB = {
    "safe": 0.52,
    "normal": 0.45,
    "risky": 0.35,
}

NORMAL_MIN_LEG = float(
    os.getenv("NORMAL_MIN_LEG", "1.30")
)

SKIP_LEAGUES = os.getenv(
    "SKIP_LEAGUES",
    "1",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

SKIP_LEAGUE_RE = re.compile(
    r"\bu-?(?:1[5-9]|2[0-3])\b|youth|reserve|women|friendl",
    re.I,
)

MAX_LEG_ODDS = {
    "safe": 2.50,
    "normal": 3.20,
    "risky": 5.00,
}

MAX_LEGS = 30

MAX_DETAIL_EVENTS = int(
    os.getenv("MAX_DETAIL_EVENTS", "100")
)

DETAIL_WORKERS = int(
    os.getenv("DETAIL_WORKERS", "12")
)

DETAIL_SECONDS = int(
    os.getenv("DETAIL_SECONDS", "40")
)

OVERSHOOT = 0.25

GROUP_LIMIT = int(
    os.getenv("GROUP_LIMIT", "200")
)


# ============================================================
# MARKETS
# ============================================================

# User does not want double chance.
ALLOW_DOUBLE_CHANCE = False

# Football-data bonus used by selection optimizer.
DATA_BONUS = 0.12

USE_AI_REVIEW = os.getenv(
    "USE_AI_REVIEW",
    "1",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

WEB_SEARCHES = int(
    os.getenv("WEB_SEARCHES", "6")
)

MAX_DROPS = 4

AI_TIMEOUT = int(
    os.getenv("AI_TIMEOUT", "50")
)

STUDY_CACHE_SECONDS = int(
    os.getenv("STUDY_CACHE_SECONDS", "1800")
)

_study_cache = {}


# IMPORTANT:
# Football data is ON by default, but now uses SOFT fallback.
USE_FOOTBALL_DATA = os.getenv(
    "USE_FOOTBALL_DATA",
    "1",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


# ============================================================
# STRAIGHT WIN SETTINGS
# ============================================================

STRAIGHT_MIN_P = float(
    os.getenv("STRAIGHT_MIN_P", "0.45")
)

STRAIGHT_GOAL_MIN_P = float(
    os.getenv("STRAIGHT_GOAL_MIN_P", "0.45")
)

STRAIGHT_1UP_MIN_P = float(
    os.getenv("STRAIGHT_1UP_MIN_P", "0.50")
)

STRAIGHT_2UP_MIN_P = float(
    os.getenv("STRAIGHT_2UP_MIN_P", "0.65")
)

STRAIGHT_2GOALS_MIN_P = float(
    os.getenv("STRAIGHT_2GOALS_MIN_P", "0.70")
)

STRAIGHT_MIN_LEGS = 13

STRAIGHT_DEFAULT_LEGS = 15


def _env_float(name, default):
    try:
        return float(
            os.getenv(name, str(default))
        )
    except Exception:
        return float(default)


NORMAL_MIN_ODD = _env_float(
    "NORMAL_MIN_ODD",
    1.20,
)

RULE_AWAY_WIN_MIN_P = _env_float(
    "RULE_AWAY_WIN_MIN_P",
    0.48,
)

RULE_HOME_WIN_P = _env_float(
    "RULE_HOME_WIN_P",
    0.55,
)

HOME_WIN_ODD_MIN = _env_float(
    "HOME_WIN_ODD_MIN",
    1.50,
)

HOME_WIN_ODD_MAX = _env_float(
    "HOME_WIN_ODD_MAX",
    1.65,
)

RULE_WIN_MIN_P = _env_float(
    "RULE_WIN_MIN_P",
    0.42,
)

RULE_DRAW_MAX_P = _env_float(
    "RULE_DRAW_MAX_P",
    0.38,
)

RULE_UNDERDOG_OPP_P = _env_float(
    "RULE_UNDERDOG_OPP_P",
    0.65,
)

RULE_TEAMGOAL_MIN_P = _env_float(
    "RULE_TEAMGOAL_MIN_P",
    0.20,
)

RULE_TEAMGOAL2_MIN_P = _env_float(
    "RULE_TEAMGOAL2_MIN_P",
    0.45,
)

RULE_BTTS_O25_MAX = _env_float(
    "RULE_BTTS_O25_MAX",
    2.00,
)

RULE_CORNERS_O25_MAX = _env_float(
    "RULE_CORNERS_O25_MAX",
    2.20,
)

RULE_OVER15_O25_MAX = _env_float(
    "RULE_OVER15_O25_MAX",
    2.60,
)

RULE_MIN_MINUTES = _env_float(
    "RULE_MIN_MINUTES",
    0,
)

RULE_MARGIN_MIN = _env_float(
    "RULE_MARGIN_MIN",
    1.02,
)

RULE_MARGIN_MAX = _env_float(
    "RULE_MARGIN_MAX",
    1.20,
)

MAX_PER_LEAGUE = int(
    _env_float("MAX_PER_LEAGUE", 2)
)

RULE_DRAW_SKIP_GAME = os.getenv(
    "RULE_DRAW_SKIP_GAME",
    "0",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

SKIP_UNRELIABLE = os.getenv(
    "SKIP_UNRELIABLE",
    "1",
).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_UNRELIABLE_RE = re.compile(
    r"\bu[-\s]?(?:1[5-9]|2[0-3])\b"
    r"|youth|reserve|academy|development"
    r"|\bwomen|\(w\)|friendl",
    re.I,
)


# ============================================================
# DAILY 2 ODDS
# ============================================================

DAILY_TARGET = 2.0
DAILY_LOW = 1.90
DAILY_HIGH = 2.40
DAILY_MAX_ODD = 1.90

DAILY_MIN_P = float(
    os.getenv("DAILY_MIN_P", "0.0")
)

DAILY_SECONDS = 24 * 60 * 60

DAILY_MIN_ODD = 1.30
DAILY_BTTS_MIN = 1.60
DAILY_CORNER_MAX = 1.50

DAILY_RE = re.compile(
    r"\bdaily\s*-?\s*2\s*-?\s*odds?\b"
    r"|^\s*best\s*2\s*odds?\s*$",
    re.I,
)

STRAIGHT_RE = re.compile(
    r"\bstraight\s*-?\s*(?:win|winning|wins)\b",
    re.I,
)


# ============================================================
# SELECTION WEIGHTS
# ============================================================

KIND_BONUS = {
    "up": 0.05,
    "corners": 0.04,
    "handicap": 0.04,
    "either_half": 0.03,
    "btts": 0.02,
    "over15": 0.02,
    "over": 0.02,
    "team_goals": 0.03,
    "win": 0.05,
}

LEG_PENALTY = 0.06

KIND_CAP = {
    "over15": 0.20,
    "over": 0.30,
    "btts": 0.25,
    "up": 0.40,
    "either_half": 0.25,
    "corners": 0.30,
    "handicap": 0.30,
    "team_goals": 0.25,
    "win": 0.30,
}

KIND_NAME = {
    "over15": "Over 1.5",
    "over": "Over goals",
    "btts": "Both teams to score",
    "up": "1UP / 2UP",
    "either_half": "Win either half",
    "corners": "Corners",
    "handicap": "Positive handicap",
    "team_goals": "Team goals",
    "win": "Home win",
    "dc12": "Home or Away (12)",
    "dnb": "Draw no bet",
    "streak": "3+ in a row",
}


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


def _safe_float(value, default=None):
    value = _f(value)
    return default if value is None else value


def _two_way(odd, other):
    if not odd or odd <= 1:
        return None

    if other and other > 1:
        a = 1 / odd
        b = 1 / other
        total = a + b

        if total:
            return a / total

    return min(0.95 / odd, 0.97)


def _single(odd):
    if not odd or odd <= 1:
        return None

    return min(0.95 / odd, 0.97)


def _side_of(outcome, two_way=False):
    label = str(
        outcome.get("desc")
        or outcome.get("name")
        or ""
    ).strip().lower()

    oid = str(outcome.get("id"))

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


def _is_positive_handicap(line):
    try:
        return float(line) > 0
    except Exception:
        return False


# ============================================================
# MARKET SAFETY
# ============================================================

def _candidate_is_allowed(kind, label=""):
    kind = str(kind or "").lower()
    label = str(label or "").lower()

    # User specifically does not want these.
    if kind in {
        "under",
        "dnb",
        "dc",
        "dc12",
        "yellow_cards",
        "cards",
    }:
        return False

    if "draw no bet" in label:
        return False

    if "under " in label:
        return False

    if "yellow card" in label:
        return False

    if "cards" in label:
        return False

    # Only positive handicaps.
    if kind == "handicap":
        match = re.search(
            r"([+-]\d+(?:\.\d+)?)\s*handicap",
            label,
        )

        if match:
            try:
                if float(match.group(1)) <= 0:
                    return False
            except Exception:
                return False

    return True


# ============================================================
# SPORTYBET MARKET EXTRACTION
# ============================================================

def event_candidates(event, markets):

    home = event.get(
        "homeTeamName",
        "Home",
    )

    away = event.get(
        "awayTeamName",
        "Away",
    )

    out = []

    def add(
        kind,
        label,
        odd,
        p,
        key,
        **extra,
    ):
        if not _candidate_is_allowed(
            kind,
            label,
        ):
            return

        odd = _safe_float(odd)
        p = _safe_float(p)

        if not odd or odd <= 1:
            return

        if not p or p <= 0:
            return

        item = {
            "kind": kind,
            "label": label,
            "odd": odd,
            "p": p,
            "key": key,
        }

        item.update(extra)
        out.append(item)

    ph = None
    pd = None
    pa = None

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

    if h and d and a:
        inv = [
            1 / h,
            1 / d,
            1 / a,
        ]

        total = sum(inv)

        if total:
            ph = inv[0] / total
            pd = inv[1] / total
            pa = inv[2] / total

            if (
                ph is not None
                and ph >= RULE_HOME_WIN_P
                and HOME_WIN_ODD_MIN <= h <= HOME_WIN_ODD_MAX
            ):
                add(
                    "win",
                    f"{home} to win",
                    h,
                    ph,
                    (
                        sp.M_1X2,
                        "",
                        sp.OUT_1X2["home"],
                    ),
                    side="home",
                    market_side_p=ph,
                    bypass=True,
                )

            for fav_side, fav_w, fav_odd in (
                ("home", ph, h),
                ("away", pa, a),
            ):
                if (
                    fav_w is not None
                    and fav_w >= RULE_HOME_WIN_P
                    and fav_odd < HOME_WIN_ODD_MIN
                ):
                    goal_pick = _team_over(
                        markets,
                        home,
                        away,
                        fav_side,
                        1.5,
                    )

                    if goal_pick:
                        add(
                            "team_goals",
                            goal_pick["label"],
                            goal_pick["odd"],
                            goal_pick["p"],
                            goal_pick["key"],
                            side=fav_side,
                            line=1.5,
                            market_side_p=fav_w,
                            bypass=True,
                        )

        # Double chance intentionally disabled.
        if ALLOW_DOUBLE_CHANCE:
            k1 = (
                sp.M_DC,
                "",
                sp.OUT_DC["1x"],
            )

            k2 = (
                sp.M_DC,
                "",
                sp.OUT_DC["x2"],
            )

            add(
                "dc",
                f"{home} or Draw",
                sp.find_odds(markets, k1),
                (
                    ph + pd
                    if ph is not None
                    else None
                ),
                k1,
                side="home",
            )

            add(
                "dc",
                f"Draw or {away}",
                sp.find_odds(markets, k2),
                (
                    pd + pa
                    if pa is not None
                    else None
                ),
                k2,
                side="away",
            )

    # --------------------------------------------------------
    # TOTAL GOALS
    # --------------------------------------------------------

    for market in markets or []:

        if str(market.get("id")) != sp.M_TOTAL:
            continue

        spec = market.get(
            "specifier"
        ) or ""

        if not spec.startswith("total="):
            continue

        line = _f(
            spec.replace(
                "total=",
                "",
            )
        )

        if line is None:
            continue

        if (line * 2) % 1 != 0:
            continue

        over = None
        under = None

        for outcome in market.get(
            "outcomes",
            [],
        ):

            if outcome.get("isActive") is False:
                continue

            oid = str(
                outcome.get("id")
            )

            if oid == sp.OUT_TOTAL["over"]:
                over = _f(
                    outcome.get("odds")
                )

            elif oid == sp.OUT_TOTAL["under"]:
                under = _f(
                    outcome.get("odds")
                )

        mid = str(
            market.get("id")
        )

        add(
            (
                "over15"
                if line == 1.5
                else "over"
            ),
            f"Over {line:g} goals",
            over,
            _two_way(
                over,
                under,
            ),
            (
                mid,
                spec,
                sp.OUT_TOTAL["over"],
            ),
            line=line,
        )

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    yes = sp.find_odds(
        markets,
        (
            sp.M_BTTS,
            "",
            sp.OUT_BTTS["yes"],
        ),
    )

    no = sp.find_odds(
        markets,
        (
            sp.M_BTTS,
            "",
            sp.OUT_BTTS["no"],
        ),
    )

    add(
        "btts",
        "Both teams to score",
        yes,
        _two_way(
            yes,
            no,
        ),
        (
            sp.M_BTTS,
            "",
            sp.OUT_BTTS["yes"],
        ),
        btts="yes",
    )

    # --------------------------------------------------------
    # OTHER MARKETS
    # --------------------------------------------------------

    for market in markets or []:

        mid = str(
            market.get("id")
        )

        if mid in (
            sp.M_1X2,
            sp.M_DC,
            sp.M_TOTAL,
            sp.M_BTTS,
        ):
            continue

        name = (
            f"{market.get('desc') or ''} "
            f"{market.get('name') or ''}"
        ).lower()

        spec = market.get(
            "specifier"
        ) or ""

        outcomes = [
            o
            for o in market.get(
                "outcomes",
                [],
            )
            if o.get("isActive") is not False
        ]

        # ----------------------------------------------------
        # 1UP / 2UP
        # ----------------------------------------------------

        up_match = re.search(
            r"1x2\W+([12])\s*-?\s*up\b",
            name,
        )

        if up_match:

            number = up_match.group(1)

            for outcome in outcomes:

                side = _side_of(
                    outcome
                )

                odd = _f(
                    outcome.get("odds")
                )

                if (
                    side not in (
                        "home",
                        "away",
                    )
                    or not odd
                ):
                    continue

                team = (
                    home
                    if side == "home"
                    else away
                )

                market_probability = (
                    ph
                    if side == "home"
                    else pa
                )

                add(
                    "up",
                    (
                        f"{team} to win "
                        f"({number}UP)"
                    ),
                    odd,
                    _single(odd),
                    (
                        mid,
                        spec,
                        str(
                            outcome.get(
                                "id"
                            )
                        ),
                    ),
                    side=side,
                    up_level=int(number),
                    market_side_p=market_probability,
                )

            continue

        # ----------------------------------------------------
        # WIN EITHER HALF
        # ----------------------------------------------------

        if (
            "either half" in name
            and not any(
                x in name
                for x in (
                    "both",
                    "1st",
                    "2nd",
                    "first",
                    "second",
                )
            )
        ):

            for side, team in (
                ("home", home),
                ("away", away),
            ):

                for outcome in outcomes:

                    label = str(
                        outcome.get("desc")
                        or outcome.get("name")
                        or ""
                    ).strip().lower()

                    odd = _f(
                        outcome.get("odds")
                    )

                    if not odd:
                        continue

                    if (
                        side in label
                        or (
                            side in name
                            and label == "yes"
                        )
                    ):

                        add(
                            "either_half",
                            (
                                f"{team} to win "
                                "either half"
                            ),
                            odd,
                            _single(odd),
                            (
                                mid,
                                spec,
                                str(
                                    outcome.get(
                                        "id"
                                    )
                                ),
                            ),
                            side=side,
                            market_side_p=(
                                ph
                                if side == "home"
                                else pa
                            ),
                        )

                        break

            continue

        # ----------------------------------------------------
        # DNB NEVER USED
        # ----------------------------------------------------

        if "draw no bet" in name:
            continue

        # ----------------------------------------------------
        # CORNERS
        # ----------------------------------------------------

        if (
            "corner" in name
            and spec.startswith("total=")
            and not any(
                x in name
                for x in (
                    "1st",
                    "2nd",
                    "first",
                    "second",
                    "half",
                    "home",
                    "away",
                    "team",
                    "race",
                    "handicap",
                    "odd",
                    "even",
                    "1x2",
                    "exact",
                    "range",
                )
            )
        ):

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line is None:
                continue

            if (line * 2) % 1 != 0:
                continue

            over = None
            over_id = None

            for outcome in outcomes:

                label = str(
                    outcome.get("desc")
                    or outcome.get("name")
                    or ""
                ).strip().lower()

                oid = str(
                    outcome.get("id")
                )

                odd = _f(
                    outcome.get("odds")
                )

                if (
                    label.startswith("over")
                    or oid == sp.OUT_TOTAL["over"]
                ):
                    over = odd
                    over_id = oid

            add(
                "corners",
                f"Over {line:g} corners",
                over,
                _single(over),
                (
                    mid,
                    spec,
                    over_id,
                ),
                line=line,
            )

            continue

        # ----------------------------------------------------
        # POSITIVE HANDICAP ONLY
        # ----------------------------------------------------

        if (
            "handicap" in name
            and "corner" not in name
            and spec.startswith("hcp=")
            and len(outcomes) == 2
            and not any(
                x in name
                for x in (
                    "1st",
                    "2nd",
                    "half",
                    "3-way",
                    "3 way",
                    "three",
                )
            )
        ):

            line = _hcp_line(
                spec
            )

            if not line:
                continue

            odds = [
                _f(
                    o.get("odds")
                )
                for o in outcomes
            ]

            for outcome, odd, other in zip(
                outcomes,
                odds,
                odds[::-1],
            ):

                side = _side_of(
                    outcome,
                    two_way=True,
                )

                if not side or not odd:
                    continue

                if side == "home" and line > 0:
                    team = home

                elif side == "away" and line < 0:
                    team = away

                else:
                    continue

                label = (
                    f"{team} "
                    f"+{abs(line):g} handicap"
                )

                add(
                    "handicap",
                    label,
                    odd,
                    _two_way(
                        odd,
                        other,
                    ),
                    (
                        mid,
                        spec,
                        str(
                            outcome.get(
                                "id"
                            )
                        ),
                    ),
                    side=side,
                    handicap=abs(line),
                )

            continue

        # ----------------------------------------------------
        # TEAM GOALS
        # ----------------------------------------------------

        if (
            "team" in name
            and "goal" in name
            and spec.startswith("total=")
            and "corner" not in name
        ):

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line is None:
                continue

            for outcome in outcomes:

                label = str(
                    outcome.get("desc")
                    or outcome.get("name")
                    or ""
                ).strip()

                low = label.lower()

                if not low.startswith(
                    "over"
                ):
                    continue

                odd = _f(
                    outcome.get("odds")
                )

                if not odd:
                    continue

                if home.lower() in name:
                    team = home
                    side = "home"

                elif away.lower() in name:
                    team = away
                    side = "away"

                else:
                    continue

                add(
                    "team_goals",
                    (
                        f"{team} Over "
                        f"{line:g} team goals"
                    ),
                    odd,
                    _single(odd),
                    (
                        mid,
                        spec,
                        str(
                            outcome.get(
                                "id"
                            )
                        ),
                    ),
                    side=side,
                    line=line,
                )

            continue

    return out


def _label_of(o):
    return str(
        o.get("desc")
        or o.get("name")
        or ""
    ).strip().lower()


def _daily_side(
    outcomes,
    o,
    three_way=False,
):

    side = _side_of(
        o,
        two_way=not three_way,
    )

    if side:
        return side

    if (
        not three_way
        and len(outcomes) == 2
    ):
        return (
            "home"
            if outcomes[0] is o
            else "away"
        )

    return None


def _hcp_line(spec):

    raw = spec.replace(
        "hcp=",
        "",
    )

    try:
        if ":" in raw:
            a, b = raw.split(":")
            return float(a) - float(b)

        return float(raw)

    except Exception:
        return None


# ============================================================
# DAILY CANDIDATES
# ============================================================

def daily_candidates(event, markets):

    home = event.get(
        "homeTeamName",
        "Home",
    )

    away = event.get(
        "awayTeamName",
        "Away",
    )

    out = []

    def add(
        kind,
        label,
        odd,
        p,
        key,
        low=None,
        high=None,
        **extra,
    ):

        # Daily mode follows the same market restrictions.
        if not _candidate_is_allowed(
            kind,
            label,
        ):
            return

        odd = _safe_float(odd)
        p = _safe_float(p)

        if not odd or not p:
            return

        if odd < (
            low
            or DAILY_MIN_ODD
        ):
            return

        if high and odd > high:
            return

        item = {
            "kind": kind,
            "label": label,
            "odd": odd,
            "p": p,
            "key": key,
        }

        item.update(extra)
        out.append(item)

    for c in event_candidates(
        event,
        markets,
    ):

        if (
            c["kind"]
            in (
                "up",
                "either_half",
                "over15",
                "btts",
                "team_goals",
                "corners",
                "handicap",
                "win",
            )
            and c["odd"] >= DAILY_MIN_ODD
        ):
            out.append(c)

    for market in markets or []:

        mid = str(
            market.get("id")
        )

        spec = market.get(
            "specifier"
        ) or ""

        outcomes = [
            o
            for o in market.get(
                "outcomes",
                [],
            )
            if o.get("isActive") is not False
        ]

        if not outcomes:
            continue

        # ----------------------------------------------------
        # HOME/AWAY 12
        # Disabled.
        # ----------------------------------------------------

        if mid == "10":
            continue

        # ----------------------------------------------------
        # TOTAL GOALS
        # ----------------------------------------------------

        if (
            mid == "18"
            and spec.startswith("total=")
        ):

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if (
                line is None
                or (line * 2) % 1 != 0
            ):
                continue

            for o in outcomes:

                if (
                    _label_of(o).startswith(
                        "over"
                    )
                    or str(
                        o.get("id")
                    )
                    == sp.OUT_TOTAL["over"]
                ):

                    odd = _f(
                        o.get("odds")
                    )

                    add(
                        "over",
                        f"Over {line:g} goals",
                        odd,
                        _single(odd),
                        (
                            mid,
                            spec,
                            str(
                                o.get("id")
                            ),
                        ),
                        line=line,
                    )

        # ----------------------------------------------------
        # BTTS
        # ----------------------------------------------------

        elif mid == "29":

            for o in outcomes:

                lab = _label_of(o)
                odd = _f(
                    o.get("odds")
                )

                if lab.startswith("yes"):
                    name = (
                        "Both teams to score"
                    )

                else:
                    continue

                add(
                    "btts",
                    name,
                    odd,
                    _single(odd),
                    (
                        mid,
                        spec,
                        str(
                            o.get("id")
                        ),
                    ),
                    low=DAILY_BTTS_MIN,
                )

        # ----------------------------------------------------
        # TEAM GOALS
        # ----------------------------------------------------

        elif (
            mid in ("19", "20")
            and spec.startswith("total=")
        ):

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

            team = (
                home
                if mid == "19"
                else away
            )

            for o in outcomes:

                if _label_of(
                    o
                ).startswith("over"):

                    odd = _f(
                        o.get("odds")
                    )

                    add(
                        "team_goals",
                        (
                            f"{team} Over "
                            f"{line:g} team goals"
                        ),
                        odd,
                        _single(odd),
                        (
                            mid,
                            spec,
                            str(
                                o.get("id")
                            ),
                        ),
                        line=line,
                    )

        # ----------------------------------------------------
        # CORNERS
        # ----------------------------------------------------

        elif (
            mid == "166"
            and spec.startswith("total=")
        ):

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line not in (
                6.5,
                7.5,
            ):
                continue

            for o in outcomes:

                if _label_of(
                    o
                ).startswith("over"):

                    odd = _f(
                        o.get("odds")
                    )

                    add(
                        "corners",
                        (
                            f"Over {line:g} "
                            "corners"
                        ),
                        odd,
                        _single(odd),
                        (
                            mid,
                            spec,
                            str(
                                o.get("id")
                            ),
                        ),
                        high=DAILY_CORNER_MAX,
                        line=line,
                    )

        # ----------------------------------------------------
        # FIRST-HALF CORNERS
        # ----------------------------------------------------

        elif (
            mid == "177"
            and spec.startswith("total=")
        ):

            line = _f(
                spec.replace(
                    "total=",
                    "",
                )
            )

            if line != 3.5:
                continue

            for o in outcomes:

                if _label_of(
                    o
                ).startswith("over"):

                    odd = _f(
                        o.get("odds")
                    )

                    add(
                        "corners",
                        "Over 3.5 corners (1st half)",
                        odd,
                        _single(odd),
                        (
                            mid,
                            spec,
                            str(
                                o.get("id")
                            ),
                        ),
                        high=DAILY_CORNER_MAX,
                        line=line,
                    )

        # ----------------------------------------------------
        # ----------------------------------------------------

        elif mid == "64":
            continue

        # ----------------------------------------------------
        # POSITIVE HANDICAP
        # ----------------------------------------------------

        elif (
            mid in ("65", "66")
            and spec.startswith("hcp=")
        ):

            line = _hcp_line(
                spec
            )

            if not line:
                continue

            three_way = mid == "65"

            for o in outcomes:

                side = _daily_side(
                    outcomes,
                    o,
                    three_way,
                )

                if (
                    side == "home"
                    and line > 0
                ):
                    team = home

                elif (
                    side == "away"
                    and line < 0
                ):
                    team = away

                else:
                    continue

                odd = _f(
                    o.get("odds")
                )

                if three_way:
                    p = _single(odd)

                else:

                    other = _f(
                        next(
                            (
                                x.get("odds")
                                for x in outcomes
                                if x is not o
                            ),
                            None,
                        )
                    )

                    p = _two_way(
                        odd,
                        other,
                    )

                add(
                    "handicap",
                    (
                        f"{team} +"
                        f"{abs(line):g} handicap"
                    ),
                    odd,
                    p,
                    (
                        mid,
                        spec,
                        str(
                            o.get("id")
                        ),
                    ),
                    side=side,
                    handicap=abs(line),
                )

    return out


def _pick_for(
    cands,
    kind,
    **attrs,
):

    for c in cands:

        if c.get("kind") != kind:
            continue

        if all(
            c.get(k) == v
            for k, v in attrs.items()
        ):
            return c

    return None


def _team_over(
    markets,
    home,
    away,
    side,
    line,
):

    want = (
        "19"
        if side == "home"
        else "20"
    )

    team = (
        home
        if side == "home"
        else away
    )

    for market in markets or []:

        if str(
            market.get("id")
        ) != want:
            continue

        spec = market.get(
            "specifier"
        ) or ""

        if not spec.startswith(
            "total="
        ):
            continue

        if (
            _f(
                spec.replace(
                    "total=",
                    "",
                )
            )
            != line
        ):
            continue

        for o in market.get(
            "outcomes",
            [],
        ):

            if o.get("isActive") is False:
                continue

            if not _label_of(
                o
            ).startswith("over"):
                continue

            odd = _f(
                o.get("odds")
            )

            if not odd or odd <= 1:
                continue

            goals = (
                1
                if line == 0.5
                else 2
            )

            return {
                "kind": "team_goals",
                "label": (
                    f"{team} to score "
                    f"{goals}+ goals "
                    f"(Over {line:g})"
                ),
                "odd": odd,
                "p": _single(odd),
                "key": (
                    want,
                    spec,
                    str(
                        o.get("id")
                    ),
                ),
                "side": side,
                "line": line,
            }

    return None


# ============================================================
# STRAIGHT WIN
# ============================================================

def straight_candidates(
    event,
    markets,
):

    home = event.get(
        "homeTeamName",
        "Home",
    )

    away = event.get(
        "awayTeamName",
        "Away",
    )

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

    if not (
        h
        and d
        and a
    ):
        return []

    inv = [
        1 / h,
        1 / d,
        1 / a,
    ]

    total = sum(inv)

    if not total:
        return []

    chances = {
        "home": inv[0] / total,
        "away": inv[2] / total,
    }

    base = event_candidates(
        event,
        markets,
    )

    out = []

    for side in (
        "home",
        "away",
    ):

        w = chances[side]

        if w < STRAIGHT_GOAL_MIN_P:
            continue

        mine = [
            c
            for c in base
            if c.get("side") == side
        ]

        def up(level):
            return _pick_for(
                mine,
                "up",
                up_level=level,
            )

        def goals(line):
            return (
                _team_over(
                    markets,
                    home,
                    away,
                    side,
                    line,
                )
                or _pick_for(
                    mine,
                    "team_goals",
                    line=line,
                )
            )

        if w >= STRAIGHT_2UP_MIN_P:

            pick = _pick_for(
                mine,
                "win",
            )

            if (
                not pick
                and w >= STRAIGHT_2GOALS_MIN_P
            ):
                pick = (
                    goals(1.5)
                    or up(2)
                    or up(1)
                )

            if not pick:
                pick = (
                    up(2)
                    or up(1)
                )

        elif w >= STRAIGHT_1UP_MIN_P:

            pick = up(1)

        else:

            pick = goals(0.5)

        if (
            pick
            and side == "away"
            and pick.get("kind") == "up"
            and w < RULE_AWAY_WIN_MIN_P
        ):
            pick = None

        if pick:

            pick = dict(pick)

            pick["market_side_p"] = w
            pick["side"] = side

            out.append(pick)

    return out


# ============================================================
# EVENT FILTER
# ============================================================

def _event_ok(
    event,
    league_name,
):

    if SKIP_UNRELIABLE:

        text = " ".join(
            [
                str(
                    league_name or ""
                ),
                str(
                    event.get(
                        "homeTeamName"
                    )
                    or ""
                ),
                str(
                    event.get(
                        "awayTeamName"
                    )
                    or ""
                ),
            ]
        )

        if _UNRELIABLE_RE.search(
            text
        ):
            return False

    start_time = (
        event.get(
            "estimateStartTime"
        )
        or 0
    )

    try:

        if (
            RULE_MIN_MINUTES > 0
            and start_time
            and (
                start_time / 1000
                - time.time()
            )
            < RULE_MIN_MINUTES * 60
        ):
            return False

    except Exception:
        pass

    return True


def _match_context(markets):

    ctx = {
        "ph": None,
        "pd": None,
        "pa": None,
        "o25": None,
        "margin": None,
    }

    try:

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

        if h and d and a:

            inv = [
                1 / h,
                1 / d,
                1 / a,
            ]

            total = sum(inv)

            if total:

                ctx["margin"] = total

                ctx["ph"] = (
                    inv[0] / total
                )

                ctx["pd"] = (
                    inv[1] / total
                )

                ctx["pa"] = (
                    inv[2] / total
                )

        ctx["o25"] = sp.find_odds(
            markets,
            (
                sp.M_TOTAL,
                "total=2.5",
                sp.OUT_TOTAL["over"],
            ),
        )

    except Exception:
        pass

    return ctx


def _passes_rules(
    c,
    ctx,
    relax=0,
):

    kind = c.get("kind")
    side = c.get("side")

    ph = ctx["ph"]
    pd = ctx["pd"]
    pa = ctx["pa"]
    o25 = ctx["o25"]

    own = (
        ph
        if side == "home"
        else pa
        if side == "away"
        else None
    )

    opp = (
        pa
        if side == "home"
        else ph
        if side == "away"
        else None
    )

    if kind in (
        "up",
        "either_half",
        "win",
    ):

        need = (
            RULE_AWAY_WIN_MIN_P
            if side == "away"
            else RULE_WIN_MIN_P
        )

        if relax >= 2:
            need = RULE_WIN_MIN_P

        if (
            own is None
            or own < need
        ):
            return False

        draw_max = (
            RULE_DRAW_MAX_P
            + (
                0.05
                if relax >= 2
                else 0
            )
        )

        if (
            pd is not None
            and pd > draw_max
        ):
            return False

    elif kind == "handicap":

        if (
            opp is not None
            and opp >= RULE_UNDERDOG_OPP_P
        ):
            return False

    elif kind == "team_goals":

        line = (
            c.get("line")
            or 0
        )

        need = (
            RULE_TEAMGOAL_MIN_P
            if line <= 0.5
            else RULE_TEAMGOAL2_MIN_P
        )

        if (
            own is not None
            and own < need
        ):
            return False

    elif kind == "btts":

        if (
            "not"
            not in str(
                c.get("label", "")
            ).lower()
        ):

            if (
                o25
                and o25 > RULE_BTTS_O25_MAX
            ):
                return False

    elif kind == "corners":

        if (
            o25
            and o25 > RULE_CORNERS_O25_MAX
        ):
            return False

    elif kind == "over15":

        if (
            o25
            and o25 > RULE_OVER15_O25_MAX
        ):
            return False

    return True


# ============================================================
# FOOTBALL DATA STUDY
# ============================================================

def study(
    groups,
    notify=None,
):

    """
    Football data is studied BEFORE the evidence gate.

    SportyBet odds determine which market exists.
    Football data determines whether the match is actually supported.
    When football data is missing we now fall back to SportyBet odds.
    """

    import football_data as fd

    if (
        not USE_FOOTBALL_DATA
        or fd.ENRICH_MAX <= 0
        or not groups
    ):
        return 0

    # ENRICH_MAX is a maximum, NOT a quota.
    top = groups[
        :fd.ENRICH_MAX
    ]

    if notify:
        try:
            notify(len(top))
        except Exception:
            pass

    studied = 0
    started = time.time()

    for group in top:

        if (
            time.time()
            - started
            > fd.ENRICH_SECONDS
        ):
            break

        try:

            event = group[0]["event"]

            event_id = event.get(
                "eventId"
            )

            hit = _study_cache.get(
                event_id
            )

            facts = None

            if (
                hit
                and (
                    time.time()
                    - hit[0]
                    < STUDY_CACHE_SECONDS
                )
            ):
                facts = hit[1]

            else:

                fixture = fd.find_fixture(
                    event
                )

                if not fixture:
                    continue

                facts = fd.get_facts(
                    fixture
                )

                if facts:
                    _study_cache[
                        event_id
                    ] = (
                        time.time(),
                        facts,
                    )

            if not facts:
                continue

            studied += 1

            for candidate in group:

                try:

                    football_probability = (
                        fd.adjusted_p(
                            candidate,
                            facts,
                        )
                    )

                except Exception:
                    football_probability = 0.0

                candidate[
                    "football_p"
                ] = max(
                    0.0,
                    min(
                        0.99,
                        float(
                            football_probability
                        ),
                    ),
                )

                # From this point forward p means
                # football-evidence probability.
                candidate["p"] = (
                    candidate[
                        "football_p"
                    ]
                )

                candidate["facts"] = facts
                candidate["has_data"] = True

        except Exception as exc:
            print(
                f"Football study failed: {exc}"
            )

    return studied


# ============================================================
# FAVOURITE / DETAIL ORDER
# ============================================================

def favourite_strength(event):

    markets = (
        event.get("markets")
        or []
    )

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

    if not (
        h
        and d
        and a
    ):
        return 0.0

    try:

        inv = [
            1 / h,
            1 / d,
            1 / a,
        ]

        total = sum(inv)

        if not total:
            return 0.0

        return max(
            inv[0],
            inv[2],
        ) / total

    except Exception:
        return 0.0


def _detail_order(events):

    if (
        len(events)
        <= MAX_DETAIL_EVENTS
    ):
        return list(events)

    ranked = sorted(
        events,
        key=favourite_strength,
        reverse=True,
    )

    selected = []
    seen = set()

    priority_count = min(
        MAX_DETAIL_EVENTS // 2,
        len(ranked),
    )

    for event in ranked[
        :priority_count
    ]:

        eid = event.get(
            "eventId"
        )

        if eid in seen:
            continue

        selected.append(event)
        seen.add(eid)

    remaining = (
        MAX_DETAIL_EVENTS
        - len(selected)
    )

    if remaining > 0:

        step = max(
            1,
            len(events)
            // remaining,
        )

        for index in range(
            0,
            len(events),
            step,
        ):

            event = events[index]

            eid = event.get(
                "eventId"
            )

            if eid in seen:
                continue

            selected.append(event)
            seen.add(eid)

            if (
                len(selected)
                >= MAX_DETAIL_EVENTS
            ):
                break

    return selected[
        :MAX_DETAIL_EVENTS
    ]


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
    straight=False,
    daily=False,
    relax=0,
):

    events = provider.get_upcoming(
        start,
        end,
    )

    if not events:
        return [], 0, 0, 0

    wanted = _detail_order(
        events
    )

    details = {}

    if wanted:

        with ThreadPoolExecutor(
            max_workers=DETAIL_WORKERS
        ) as pool:

            futures = {}

            for event in wanted:

                try:

                    event_id = event[
                        "eventId"
                    ]

                    futures[
                        pool.submit(
                            provider
                            ._event_markets_cached,
                            event_id,
                        )
                    ] = event

                except Exception:
                    continue

            try:

                for future in as_completed(
                    futures,
                    timeout=DETAIL_SECONDS,
                ):

                    event = futures[
                        future
                    ]

                    try:

                        details[
                            event["eventId"]
                        ] = future.result()

                    except Exception:
                        pass

            except FutureTimeout:
                pass

    min_p = (
        MIN_LEG_PROB.get(
            risk,
            MIN_LEG_PROB["normal"],
        )
        - min_p_shift
    )

    max_odd = MAX_LEG_ODDS.get(
        risk,
        MAX_LEG_ODDS["normal"],
    )

    post_min_p = min_p

    if straight:

        min_p = 0.30
        post_min_p = STRAIGHT_MIN_P
        max_odd = 2.60

    if daily:

        min_p = 0.30
        post_min_p = DAILY_MIN_P
        max_odd = DAILY_MAX_ODD

    groups = []

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # We DO NOT filter candidates by odds-derived p here.
    #
    # We first build valid market candidates.
    # Then football_data studies the match.
    # Then the football probability is used (or SportyBet fallback).
    # --------------------------------------------------------

    for event in events:

        markets = (
            details.get(
                event["eventId"]
            )
            or event.get("markets")
            or []
        )

        try:
            fixture = sp.sporty_fixture(
                event
            )
        except Exception:
            fixture = {
                "league": {
                    "name": "Football"
                }
            }

        league_name = str(
            (
                fixture.get(
                    "league",
                    {},
                )
                or {}
            ).get(
                "name",
                "",
            )
        )

        if (
            SKIP_LEAGUES
            and SKIP_LEAGUE_RE.search(
                league_name
            )
        ):
            continue

        if not _event_ok(
            event,
            league_name,
        ):
            continue

        ctx = _match_context(
            markets
        )

        margin = ctx.get(
            "margin"
        )

        if (
            margin is not None
            and not (
                RULE_MARGIN_MIN
                <= margin
                <= RULE_MARGIN_MAX
            )
        ):
            continue

        if (
            RULE_DRAW_SKIP_GAME
            and ctx["pd"] is not None
            and ctx["pd"]
            > RULE_DRAW_MAX_P
        ):
            continue

        if daily:
            source = daily_candidates(
                event,
                markets,
            )

        elif straight:
            source = straight_candidates(
                event,
                markets,
            )

        else:
            source = event_candidates(
                event,
                markets,
            )

        kept = []
        seen = set()

        for candidate in source:

            if not _candidate_is_allowed(
                candidate["kind"],
                candidate["label"],
            ):
                continue

            # Rule checks that do not require football data.
            if not straight and not _passes_rules(
                candidate,
                ctx,
                relax,
            ):
                continue

            if not (
                floor
                <= candidate["odd"]
                <= max_odd
            ):
                continue

            key = (
                event["eventId"],
                candidate["key"],
            )

            if key in exclude:
                continue

            if candidate["label"] in seen:
                continue

            seen.add(
                candidate["label"]
            )

            start_time = (
                event.get(
                    "estimateStartTime"
                )
                or 0
            )

            try:

                kickoff = datetime.fromtimestamp(
                    start_time / 1000,
                    tz=timezone.utc,
                )

            except Exception:

                kickoff = datetime.now(
                    timezone.utc
                )

            league_data = (
                fixture.get(
                    "league",
                    {},
                )
                or {}
            )

            # At this point p is still only
            # a temporary market value.
            # It will be replaced by football_data if available.
            candidate.update(
                {
                    "event": event,
                    "event_id": event[
                        "eventId"
                    ],
                    "home": event.get(
                        "homeTeamName",
                        "Home",
                    ),
                    "away": event.get(
                        "awayTeamName",
                        "Away",
                    ),
                    "kickoff": kickoff,
                    "league": league_data.get(
                        "name",
                        "Football",
                    ),
                    "has_data": False,
                    "facts": None,
                    "ctx": ctx,
                    "market_p": candidate.get(
                        "p",
                        0.0,
                    ),
                    "win_p": max(
                        ctx["ph"] or 0,
                        ctx["pa"] or 0,
                    ),
                }
            )

            kept.append(
                candidate
            )

        if kept:
            groups.append(
                kept
            )

    # --------------------------------------------------------
    # Study football BEFORE sorting/filtering by probability.
    # --------------------------------------------------------

    if USE_FOOTBALL_DATA:
        studied = study(
            groups,
            notify,
        )

    else:
        # Compatibility mode only.
        studied = 0

        for group in groups:
            for candidate in group:

                candidate[
                    "has_data"
                ] = True

                candidate[
                    "facts"
                ] = None

                candidate[
                    "football_p"
                ] = candidate.get(
                    "market_p",
                    0.0,
                )

                candidate[
                    "p"
                ] = candidate[
                    "football_p"
                ]

    # --------------------------------------------------------
    # SOFT evidence gate â keep picks even without football data
    # --------------------------------------------------------

    evidence_groups = []

    for group in groups:

        valid = []

        for candidate in group:

            # Soft fallback: when no football data, use SportyBet market probability
            if USE_FOOTBALL_DATA and not candidate.get("has_data"):
                candidate["p"] = candidate.get("market_p", 0.45)
                # keep has_data = False so we know it is a fallback

            if (
                candidate.get(
                    "p",
                    0,
                )
                < post_min_p
                and not candidate.get(
                    "bypass"
                )
            ):
                continue

            valid.append(
                candidate
            )

        if valid:
            evidence_groups.append(
                valid
            )

    # Sort AFTER football study / fallback.
    evidence_groups.sort(
        key=lambda group: max(
            c.get("p", 0)
            for c in group
        ),
        reverse=True,
    )

    evidence_groups = evidence_groups[
        :GROUP_LIMIT
    ]

    return (
        evidence_groups,
        len(events),
        len(details),
        studied,
    )


# ============================================================
# DYNAMIC PROGRAMMING
# ============================================================

def _dp(
    groups,
    target,
):

    if not groups or not target:
        return None

    S = 200

    try:

        tw = math.ceil(
            math.log(target)
            * S
        )

        wm = max(
            tw,
            int(
                math.log(
                    target
                    * (
                        1
                        + OVERSHOOT
                    )
                )
                * S
            ),
        )

    except Exception:
        return None

    INF = float("inf")

    dp = [INF] * (
        wm + 1
    )

    dp[0] = 0.0

    choices = []

    for group in groups:

        new = dp[:]

        choice = [
            None
        ] * (
            wm + 1
        )

        for oi, candidate in enumerate(
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

            try:

                weight = round(
                    math.log(odd)
                    * S
                )

                cost = (
                    -math.log(
                        probability
                    )
                    - KIND_BONUS.get(
                        candidate.get(
                            "kind"
                        ),
                        0.0,
                    )
                    + LEG_PENALTY
                    - (
                        DATA_BONUS
                        if candidate.get(
                            "has_data"
                        )
                        else 0.0
                    )
                )

            except Exception:
                continue

            if (
                weight <= 0
                or weight > wm
            ):
                continue

            for x in range(
                0,
                wm - weight + 1,
            ):

                base = dp[x]

                if base == INF:
                    continue

                value = (
                    base + cost
                )

                if value < new[
                    x + weight
                ]:

                    new[
                        x + weight
                    ] = value

                    choice[
                        x + weight
                    ] = (
                        oi,
                        x,
                    )

        dp = new

        choices.append(
            choice
        )

    best = None

    for x in range(
        tw,
        wm + 1,
    ):

        if (
            dp[x] < INF
            and (
                best is None
                or dp[x]
                < dp[best]
            )
        ):
            best = x

    if best is None:
        return None

    picked = []

    x = best

    for gi in range(
        len(groups) - 1,
        -1,
        -1,
    ):

        step = choices[
            gi
        ][x]

        if step is None:
            continue

        index, previous = step

        picked.append(
            groups[gi][index]
        )

        x = previous

    picked.reverse()

    return picked[
        :MAX_LEGS
    ]


def _product(picks):

    total = 1.0

    for candidate in picks:
        total *= candidate[
            "odd"
        ]

    return total


def _dp_exact(
    groups,
    target,
):

    if not groups:
        return None

    aim = target
    best = None

    for _ in range(6):

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
                < _product(best)
            )
        ):
            best = picks

        if (
            target
            <= actual
            <= target
            * (
                1
                + OVERSHOOT
                + 0.02
            )
        ):
            return picks

        if actual <= 0:
            break

        aim = max(
            target,
            aim
            * target
            / actual
            * 1.004,
        )

    return best


def _prune(
    groups,
    legs,
    scale,
):

    drop = set()

    for kind, share in KIND_CAP.items():

        cap = max(
            1,
            math.ceil(
                share
                * legs
                * scale
            ),
        )

        ranked = sorted(
            (
                candidate
                for group in groups
                for candidate in group
                if candidate[
                    "kind"
                ] == kind
            ),
            key=lambda candidate: candidate.get(
                "p",
                0,
            ),
            reverse=True,
        )

        drop.update(
            id(candidate)
            for candidate in ranked[
                cap:
            ]
        )

    pruned = []

    for group in groups:

        kept = [
            candidate
            for candidate in group
            if id(candidate)
            not in drop
        ]

        if kept:
            pruned.append(
                kept
            )

    return pruned


def _kind_counts(chosen):

    counts = {}

    for candidate in chosen:

        kind = candidate[
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


def choose_target(
    groups,
    target,
):

    if not groups or not target:
        return [], False

    all_weights = sorted(
        math.log(
            candidate["odd"]
        )
        for group in groups
        for candidate in group
        if candidate.get(
            "odd",
            0,
        ) > 1
    )

    if not all_weights:
        return [], False

    median_weight = all_weights[
        len(all_weights) // 2
    ]

    legs = max(
        3,
        min(
            MAX_LEGS,
            round(
                math.log(target)
                / max(
                    median_weight,
                    0.05,
                )
            ),
        ),
    )

    scale = 1.0

    for _ in range(5):

        for _ in range(4):

            pruned = _prune(
                groups,
                legs,
                scale,
            )

            if not pruned:
                scale *= 1.6
                continue

            chosen = _dp_exact(
                pruned,
                target,
            )

            if chosen:
                break

            scale *= 1.6

        else:
            chosen = None

        if not chosen:
            break

        counts = _kind_counts(
            chosen
        )

        valid_mix = True

        for kind, share in KIND_CAP.items():

            allowed = max(
                1,
                math.ceil(
                    share
                    * len(chosen)
                    * scale
                ),
            )

            if (
                counts.get(
                    kind,
                    0,
                )
                > allowed
            ):
                valid_mix = False
                break

        if valid_mix:
            return chosen, True

        legs = len(chosen)

    return [], False


def choose_count(
    groups,
    count,
):

    if not groups or count <= 0:
        return [], False

    count = min(
        int(count),
        MAX_LEGS,
    )

    # Soft: no longer require has_data
    ranked = sorted(
        (
            candidate
            for group in groups
            for candidate in group
        ),
        key=lambda candidate: (
            candidate.get(
                "p",
                0,
            )
            + 0.25
            * candidate.get(
                "win_p",
                0,
            )
            + KIND_BONUS.get(
                candidate.get(
                    "kind"
                ),
                0,
            )
            + (
                DATA_BONUS
                if candidate.get(
                    "has_data"
                )
                else 0
            )
        ),
        reverse=True,
    )

    chosen = []

    for scale in (
        1.0,
        1.6,
        2.5,
        4.0,
    ):

        chosen = []
        used_events = set()
        counts = {}

        for candidate in ranked:

            event_id = candidate[
                "event_id"
            ]

            if event_id in used_events:
                continue

            kind = candidate[
                "kind"
            ]

            cap = max(
                1,
                math.ceil(
                    KIND_CAP.get(
                        kind,
                        0.30,
                    )
                    * count
                    * scale
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

            if (
                len(chosen)
                >= count
            ):
                return chosen, True

    return (
        chosen,
        len(chosen) >= count,
    )


def choose_straight(
    groups,
    target,
    count,
):

    best = []

    for group in groups:

        usable = [
            c
            for c in group
            if c.get(
                "p",
                0,
            ) > 0
        ]

        if usable:
            best.append(
                max(
                    usable,
                    key=lambda c: c.get(
                        "p",
                        0,
                    ),
                )
            )

    best.sort(
        key=lambda c: c.get(
            "p",
            0,
        ),
        reverse=True,
    )

    if not best:
        return [], False

    if count:

        count = min(
            int(count),
            MAX_LEGS,
        )

        return (
            best[:count],
            len(best) >= count,
        )

    if target:

        chosen = []
        total = 1.0

        for candidate in best[
            :MAX_LEGS
        ]:

            chosen.append(
                candidate
            )

            total *= candidate[
                "odd"
            ]

            if total >= target:
                return chosen, True

        return chosen, False

    return (
        best[
            :STRAIGHT_DEFAULT_LEGS
        ],
        True,
    )


def choose_daily(groups):

    import itertools

    best = []

    for group in groups:

        usable = [
            c
            for c in group
            if c.get(
                "p",
                0,
            ) > 0
        ]

        if usable:
            best.append(
                max(
                    usable,
                    key=lambda c: c.get(
                        "p",
                        0,
                    ),
                )
            )

    best.sort(
        key=lambda c: c.get(
            "p",
            0,
        ),
        reverse=True,
    )

    pool = best[:25]

    for low, high in (
        (
            DAILY_LOW,
            DAILY_HIGH,
        ),
        (
            1.70,
            2.80,
        ),
    ):

        top = None
        top_chance = 0.0

        for size in (
            1,
            2,
            3,
        ):

            for combo in itertools.combinations(
                pool,
                size,
            ):

                total = 1.0
                chance = 1.0

                for candidate in combo:

                    total *= candidate[
                        "odd"
                    ]

                    chance *= candidate[
                        "p"
                    ]

                if not (
                    low
                    <= total
                    <= high
                ):
                    continue

                if chance > top_chance:

                    top = combo
                    top_chance = chance

        if top:
            return (
                list(top),
                True,
            )

    return [], False


# ============================================================
# AI REVIEW
# ============================================================

def _ai_json(
    prompt,
    system,
):

    if not ANTHROPIC_API_KEY:
        return None

    for use_tools in (
        True,
        False,
    ):

        body = {
            "model": AI_MODEL,
            "max_tokens": 3500,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        if use_tools:

            body["tools"] = [
                {
                    "type":
                        "web_search_20250305",
                    "name":
                        "web_search",
                    "max_uses":
                        WEB_SEARCHES,
                }
            ]

        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(
                body
            ).encode("utf-8"),
            method="POST",
            headers={
                "content-type":
                    "application/json",
                "x-api-key":
                    ANTHROPIC_API_KEY,
                "anthropic-version":
                    "2023-06-01",
            },
        )

        try:

            wait = (
                AI_TIMEOUT
                if use_tools
                else 25
            )

            with urlopen(
                request,
                timeout=wait,
            ) as response:

                data = json.loads(
                    response.read()
                    .decode("utf-8")
                )

        except Exception as exc:

            print(
                f"AI review failed: {exc}"
            )

            continue

        text = "".join(
            block.get(
                "text",
                "",
            )
            for block in data.get(
                "content",
                [],
            )
            if block.get(
                "type"
            ) == "text"
        )

        parsed = _parse_ai_json(
            text
        )

        if parsed:
            return parsed

    return None


REVIEW_SYSTEM = """
You are the football analyst behind SportyTips.

You receive football selections that have already been supported by
football-data analysis.

For each pick, search the web for the latest team news, recent form,
injuries, suspensions, rotation and motivation, and use your football
knowledge of the teams.

Rules:

- Only state things you found or know with confidence.
- Never invent statistics, injuries, scores or results.
- If you cannot find useful news for a match, say so briefly.
- Do not use the SportyBet price as the reason for a pick.
- SportyBet price is only market context.
- Judge whether the actual football selection makes sense.
- A weak team, missing key players, rotation or poor recent form can be
  a reason to drop a selection.
- Do not guarantee a result.
- Write one short reason per pick, under 200 characters.
- Mark "drop" only when football information clearly hurts the selection.
- Otherwise mark "keep".

Reply ONLY with JSON:

{"picks":[{"i":0,"reason":"...","flag":"keep"}]}
"""


def ai_review(
    chosen,
    local_now,
):

    lines = []

    for i, candidate in enumerate(
        chosen
    ):

        when = candidate[
            "kickoff"
        ].astimezone(
            bot.LOCAL_TZ
        ).strftime(
            "%a %H:%M"
        )

        lines.append(
            f"{i}. "
            f"{candidate['home']} vs "
            f"{candidate['away']} "
            f"({candidate['league']}) "
            f"kickoff {when} | "
            f"pick: {candidate['label']} | "
            f"SportyBet price "
            f"{candidate['odd']:.2f}"
        )

    prompt = (
        "Today is "
        f"{local_now.strftime('%A %d %B %Y')} "
        "(Nigeria time).\n\n"
        + "\n".join(lines)
    )

    data = _ai_json(
        prompt,
        REVIEW_SYSTEM,
    )

    reasons = {}

    if not data:
        return reasons

    items = data.get(
        "picks",
        [],
    )

    if not isinstance(
        items,
        list,
    ):
        return reasons

    for item in items:

        try:
            index = int(
                item.get("i")
            )
        except (
            TypeError,
            ValueError,
            AttributeError,
        ):
            continue

        if not (
            0
            <= index
            < len(chosen)
        ):
            continue

        reason = str(
            item.get(
                "reason",
                "",
            )
        ).strip()[:240]

        flag = (
            "drop"
            if (
                str(
                    item.get(
                        "flag",
                        "",
                    )
                ).lower()
                == "drop"
                and reason
            )
            else "keep"
        )

        if reason:

            candidate = chosen[
                index
            ]

            reasons[
                (
                    candidate[
                        "event_id"
                    ],
                    candidate[
                        "key"
                    ],
                )
            ] = (
                reason,
                flag,
            )

    return reasons


# ============================================================
# FOOTBALL REASON
# ============================================================

def _reason_for(
    candidate,
):

    facts = candidate.get(
        "facts"
    )

    if facts:

        try:

            import football_data as fd

            return fd.reason_for(
                candidate,
                facts,
                candidate[
                    "home"
                ],
                candidate[
                    "away"
                ],
            )

        except Exception:
            pass

    # Do NOT describe SportyBet odds as football evidence.
    p = round(
        candidate.get(
            "p",
            0,
        )
        * 100
    )

    side_p = candidate.get(
        "market_side_p"
    )

    side = candidate.get(
        "side"
    )

    if (
        side_p
        and side
        in (
            "home",
            "away",
        )
    ):

        team = (
            candidate["home"]
            if side == "home"
            else candidate["away"]
        )

        return (
            f"SportyBet market support "
            f"for {team} is about "
            f"{p}% for this market."
        )

    return (
        "SportyBet market analysis "
        f"supports this selection at "
        f"about {p}%."
    )


# ============================================================
# RELAXATION
# ============================================================

def _relax_note(level):

    if level == 1:
        return (
            "I loosened one selection rule "
            "to fill your ticket: up to "
            f"{MAX_PER_LEAGUE + 1} picks per league."
        )

    if level >= 2:
        return (
            "I loosened selection rules to "
            "find enough football-supported "
            "picks: up to "
            f"{MAX_PER_LEAGUE + 2} picks per league."
        )

    return ""


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
    straight=False,
    daily=False,
):

    best = None
    best_level = 0

    for level in range(3):

        built = _build_once(
            provider,
            req,
            target,
            count,
            risk,
            exclude,
            notify,
            straight,
            daily,
            level,
        )

        chosen = built[
            "chosen"
        ]

        if (
            best is None
            or len(chosen)
            > len(
                best["chosen"]
            )
        ):

            best = built
            best_level = level

        short = (
            not chosen
            or (
                count
                and len(chosen)
                < count
            )
            or (
                target
                and not built[
                    "reached"
                ]
            )
        )

        if not short:

            best = built
            best_level = level
            break

    if (
        best_level
        and best["chosen"]
    ):

        note = _relax_note(
            best_level
        )

        best["note"] = (
            (
                best["note"]
                + " "
                if best["note"]
                else ""
            )
            + note
        )

    return best


def _fmt(value):

    try:
        value = float(value)
    except Exception:
        return str(value)

    if value.is_integer():
        return str(
            int(value)
        )

    return f"{value:g}"


def _time_text(
    candidate,
    today,
):

    local = candidate[
        "kickoff"
    ].astimezone(
        bot.LOCAL_TZ
    )

    text = (
        local.strftime(
            "%I:%M %p"
        )
        .lstrip("0")
    )

    if local.date() == today:
        return text

    return (
        local.strftime("%a ")
        + text
    )


def _confidence(p):

    if p >= 0.80:
        return "ð¢"

    if p >= 0.70:
        return "ð¡"

    return "ð "


def _excess(
    chosen,
    relax=0,
):

    drop = []

    cap = (
        MAX_PER_LEAGUE
        + relax
        if MAX_PER_LEAGUE > 0
        else 0
    )

    if cap > 0:

        by = {}

        for c in chosen:

            by.setdefault(
                c.get("league"),
                [],
            ).append(c)

        for items in by.values():

            if len(items) > cap:

                items.sort(
                    key=lambda c:
                        c.get(
                            "p",
                            0,
                        ),
                    reverse=True,
                )

                drop.extend(
                    items[cap:]
                )

    gone = {
        id(c)
        for c in drop
    }

    seen = set()

    for c in sorted(
        chosen,
        key=lambda c:
            c.get(
                "p",
                0,
            ),
        reverse=True,
    ):

        if id(c) in gone:
            continue

        teams = {
            str(
                c.get(
                    "home",
                    "",
                )
            ).lower(),
            str(
                c.get(
                    "away",
                    "",
                )
            ).lower(),
        }

        if teams & seen:

            drop.append(c)
            gone.add(
                id(c)
            )

            continue

        seen |= teams

    return drop


# ============================================================
# ONE BUILD
# ============================================================

def _build_once(
    provider,
    req,
    target,
    count,
    risk,
    exclude=frozenset(),
    notify=None,
    straight=False,
    daily=False,
    relax=0,
):

    floor = NORMAL_MIN_LEG

    if risk == "risky":
        floor = max(
            floor,
            1.35,
        )

    floor = max(
        floor,
        NORMAL_MIN_ODD,
    )

    if straight:
        floor = 1.05

    if daily:
        floor = DAILY_MIN_ODD

    extra_days = 0
    note = ""

    while extra_days <= 4:

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
            (
                0.05
                if (
                    target
                    and target <= 20
                )
                else 0.0
            ),
            notify,
            straight=straight,
            daily=daily,
            relax=relax,
        )

        if not groups:

            extra_days += 1
            continue

        def pick(gs):

            if daily:
                return choose_daily(
                    gs
                )

            if straight:
                return choose_straight(
                    gs,
                    target,
                    count,
                )

            if target:
                return choose_target(
                    gs,
                    target,
                )

            return choose_count(
                gs,
                count,
            )

        chosen, reached = pick(
            groups
        )

        capped = groups

        for _ in range(8):

            drop = _excess(
                chosen,
                relax,
            )

            if not drop:
                break

            banned = {
                c["event_id"]
                for c in drop
            }

            capped = [
                g
                for g in capped
                if g[0][
                    "event_id"
                ]
                not in banned
            ]

            if not capped:
                break

            trial, ok = pick(
                capped
            )

            if not trial:
                break

            chosen = trial
            reached = ok

        too_few = (
            straight
            and not target
            and len(chosen)
            < (
                count
                or STRAIGHT_MIN_LEGS
            )
            and extra_days < 2
        )

        if chosen and not too_few:
            break

        extra_days += 1

    else:

        chosen = []
        reached = False
        total_events = 0
        detailed = 0
        studied = 0

    if extra_days and chosen:

        note = (
            "I also used matches from "
            f"the next {extra_days} day"
            f"{'s' if extra_days > 1 else ''} "
            "to reach your target."
        )

    return {
        "chosen": chosen,
        "reached": reached,
        "note": note,
        "events": total_events,
        "detailed": detailed,
        "floor": floor,
        "studied": studied,
    }


# ============================================================
# IMAGE
# ============================================================

def _launcher_send_photo(
    chat_id,
    png,
    caption="",
):

    try:

        launcher = importlib.import_module(
            os.getenv(
                "LAUNCHER_MODULE",
                "launcher",
            )
        )

        send = getattr(
            launcher,
            "send_photo",
            None,
        )

        if send:
            send(
                chat_id,
                png,
                caption,
            )

    except Exception as exc:
        print(
            f"Ticket picture failed: {exc}"
        )


# ============================================================
# MAIN FLOW
# ============================================================

def _flow_main(
    chat_id,
    text,
    search_days=None,
    target_override=None,
    count_override=None,
    daily=False,
    **kwargs,
):

    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    if provider is None:

        bot.send_message(
            chat_id,
            "â SportyBet mode is off on this server.",
        )

        return

    try:

        req = bot.parse_request(
            text
        )

    except Exception as exc:

        print(
            f"parse_request failed: {exc}"
        )

        bot.send_message(
            chat_id,
            (
                "â I couldn't understand "
                "that request. Try: "
                "10 odds today"
            ),
        )

        return

    if (
        search_days
        and req.get("label")
        == "next 24 hours"
    ):

        try:

            days = int(
                search_days
            )

            if days > 1:

                req["end"] = (
                    req["start"]
                    + timedelta(
                        days=days
                    )
                )

                req["label"] = (
                    f"next {days} days"
                )

        except Exception:
            pass

    target = (
        target_override
        or req.get(
            "target_odds"
        )
    )

    count = (
        count_override
        or req.get(
            "picks"
        )
    )

    risk = (
        req.get("risk")
        or "normal"
    )

    straight = bool(
        STRAIGHT_RE.search(
            text or ""
        )
    )

    if (
        straight
        and re.search(
            r"\blong\b",
            text or "",
            re.I,
        )
        and req.get(
            "label"
        )
        == "next 24 hours"
    ):

        req["end"] = (
            req["start"]
            + timedelta(
                days=5
            )
        )

        req["label"] = (
            "next 5 days"
        )

    if not target:

        match = re.search(
            r"(\d+(?:\.\d+)?)"
            r"\s*(?:total\s*)?"
            r"odds?\b",
            text or "",
            re.I,
        )

        if match:

            try:

                value = float(
                    match.group(1)
                )

                if value >= 1.5:

                    target = value
                    count = None

            except Exception:
                pass

    if daily:

        target = DAILY_TARGET
        count = None
        risk = "safe"

    if (
        straight
        and not target
        and not count
    ):

        count = (
            STRAIGHT_DEFAULT_LEGS
        )

        if (
            req.get("label")
            == "next 24 hours"
        ):

            req["end"] = (
                req["start"]
                + timedelta(
                    days=3
                )
            )

            req["label"] = (
                "next 3 days"
            )

    elif (
        not target
        and not count
    ):

        count = 5

    try:

        built = build_ticket(
            provider,
            req,
            target,
            count,
            risk,
            notify=None,
            straight=straight,
            daily=daily,
        )

    except Exception as exc:

        print(
            f"Smart ticket failed: "
            f"{type(exc).__name__}: {exc}"
        )

        import traceback

        traceback.print_exc()

        bot.send_message(
            chat_id,
            (
                "â Something went wrong "
                "while building the ticket. "
                "Please try again in a minute."
            ),
        )

        return

    chosen = built[
        "chosen"
    ]

    if (
        not chosen
        and risk != "risky"
        and not straight
        and not daily
    ):

        try:

            retry = build_ticket(
                provider,
                req,
                target,
                count,
                "risky",
                notify=None,
            )

            if retry[
                "chosen"
            ]:

                built = retry
                chosen = retry[
                    "chosen"
                ]

                built["note"] = (
                    (
                        built["note"]
                        + " "
                        if built["note"]
                        else ""
                    )
                    + "I widened the market limits "
                    "to find more picks."
                )

        except Exception as exc:
            print(
                f"Wider retry failed: {exc}"
            )

    local_now = (
        datetime.now(
            timezone.utc
        ).astimezone(
            bot.LOCAL_TZ
        )
    )

    if not chosen and daily:

        bot.send_message(
            chat_id,
            (
                "â I couldn't find a "
                "Daily 2 odds combination "
                "right now. Try again later."
            ),
        )

        return

    if not chosen and straight:

        bot.send_message(
            chat_id,
            (
                "â I couldn't find enough "
                "straight win picks right now. "
                "Try again later or ask for "
                "a straight win long ticket."
            ),
        )

        return

    if not chosen:

        bot.send_message(
            chat_id,
            (
                "â I couldn't build a ticket "
                "from the available football "
                "matches for that request. "
                "Try a different time window "
                "or odds."
            ),
        )

        return

    # ========================================================
    # AI REVIEW
    # ========================================================

    reasons = {}
    swapped = []
    reviewed = False

    if (
        USE_AI_REVIEW
        and ANTHROPIC_API_KEY
        and not straight
    ):

        try:

            for round_no in range(2):

                fresh = [
                    candidate
                    for candidate in chosen
                    if (
                        candidate[
                            "event_id"
                        ],
                        candidate[
                            "key"
                        ],
                    )
                    not in reasons
                ]

                if fresh:

                    reasons.update(
                        ai_review(
                            fresh,
                            local_now,
                        )
                    )

                if not reasons:
                    break

                reviewed = True

                if round_no == 1:
                    break

                drops = [
                    candidate
                    for candidate in chosen
                    if reasons.get(
                        (
                            candidate[
                                "event_id"
                            ],
                            candidate[
                                "key"
                            ],
                        ),
                        (
                            "",
                            "keep",
                        ),
                    )[1]
                    == "drop"
                ][:MAX_DROPS]

                if not drops:
                    break

                exclude = frozenset(
                    (
                        candidate[
                            "event_id"
                        ],
                        candidate[
                            "key"
                        ],
                    )
                    for candidate in drops
                )

                rebuilt = build_ticket(
                    provider,
                    req,
                    target,
                    count,
                    risk,
                    exclude=exclude,
                    straight=straight,
                    daily=daily,
                )

                if not rebuilt[
                    "chosen"
                ]:
                    break

                swapped = [
                    (
                        candidate,
                        reasons[
                            (
                                candidate[
                                    "event_id"
                                ],
                                candidate[
                                    "key"
                                ],
                            )
                        ][0],
                    )
                    for candidate in drops
                    if (
                        candidate[
                            "event_id"
                        ],
                        candidate[
                            "key"
                        ],
                    )
                    in reasons
                ]

                built = rebuilt
                chosen = rebuilt[
                    "chosen"
                ]

        except Exception as exc:

            print(
                f"AI review step failed: {exc}"
            )

    # ========================================================
    # FINAL SORT
    # ========================================================

    chosen.sort(
        key=lambda c:
            c["kickoff"]
    )

    total_odds = 1.0
    chance = 1.0

    for candidate in chosen:

        total_odds *= candidate[
            "odd"
        ]

        chance *= candidate[
            "p"
        ]

        candidate[
            "reason"
        ] = reasons.get(
            (
                candidate[
                    "event_id"
                ],
                candidate[
                    "key"
                ],
            ),
            (
                _reason_for(
                    candidate
                ),
                "keep",
            ),
        )[0]

    # ========================================================
    # REAL SPORTYBET BOOKING CODE
    # ========================================================

    code = None

    try:

        code = provider.create_booking_code(
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

    except Exception as exc:

        print(
            f"Booking code failed: {exc}"
        )

    # ========================================================
    # TELEGRAM OUTPUT
    # ========================================================

    today = local_now.date()

    if daily:

        title = (
            "DAILY 2 ODDS"
        )

    elif straight:

        title = (
            f"STRAIGHT WIN - "
            f"{len(chosen)} PICKS"
        )

    elif target:

        title = (
            f"{_fmt(target)} ODDS"
        )

    else:

        title = (
            f"{len(chosen)} PICKS"
        )

    lines = [
        (
            "ð¯ <b>SPORTYTIPS - "
            f"{html.escape(title)}</b>"
        ),
        (
            f"ð "
            f"{html.escape(req['label'].capitalize())}"
            f" â¢ times in "
            f"{bot.LOCAL_TZ_NAME}"
        ),
    ]

    for number, candidate in enumerate(
        chosen,
        start=1,
    ):

        lines.append("")

        lines.append(
            f"<b>{number}.</b> "
            f"ð "
            f"{html.escape(_time_text(candidate, today))}"
            f" â¢ ð "
            f"{html.escape(candidate['league'])}"
        )

        lines.append(
            f"â½ "
            f"{html.escape(candidate['home'])}"
            f" vs "
            f"{html.escape(candidate['away'])}"
        )

        lines.append(
            f"â <b>"
            f"{html.escape(candidate['label'])}"
            f"</b>"
            f" â¢ ð° "
            f"{candidate['odd']:.2f}"
            f" â¢ "
            f"{_confidence(candidate['p'])}"
            f" "
            f"{round(candidate['p'] * 100)}%"
        )

        lines.append(
            f"ð¬ "
            f"{html.escape(candidate['reason'])}"
        )

    lines += [
        "",
        "ââââââââââââ",
        (
            "ð° <b>Total odds: "
            f"{total_odds:.2f}</b>"
        ),
        (
            "ð Estimated combined chance: "
            f"<b>{chance * 100:.1f}%</b>"
        ),
    ]

    if (
        target
        and not built[
            "reached"
        ]
    ):

        lines.append(
            (
                f"â ï¸ I could not safely reach "
                f"{_fmt(target)} odds with "
                "the available selections."
            )
        )

    if built["note"]:

        lines.append(
            f"â¹ï¸ "
            f"{html.escape(built['note'])}"
        )

    for candidate, why in swapped:

        lines.append(
            (
                "ð Swapped "
                f"{html.escape(candidate['home'])} "
                "vs "
                f"{html.escape(candidate['away'])}: "
                f"{html.escape(why)}"
            )
        )

    mix = _kind_counts(
        chosen
    )

    lines.append(
        "ð§© Mix: "
        + ", ".join(
            f"{number} "
            f"{KIND_NAME.get(kind, kind).lower()}"
            for kind, number
            in sorted(
                mix.items(),
                key=lambda x:
                    -x[1],
            )
        )
    )

    if code:

        lines.append(
            (
                "ð² SportyBet code: "
                f"<b>"
                f"{html.escape(str(code))}"
                f"</b>"
            )
        )

    else:

        lines.append(
            "â ï¸ SportyBet booking code "
            "could not be created."
        )

    if reviewed:

        lines.append(
            (
                "ð¬ Picks were additionally "
                "checked for current team news, "
                "form and squad information. "
                "Check confirmed line-ups before kickoff."
            )
        )

    if (
        len(chosen) >= 12
        and target
        and target >= 50
    ):

        lines.append(
            (
                "â ï¸ Big-odds tickets are naturally "
                "high risk. Stake responsibly."
            )
        )

    if daily:

        lines.append(
            (
                "ð Daily 2 odds updates once "
                "every 24 hours. "
                "No pick is ever 100% certain."
            )
        )

    lines.append(
        "â ï¸ Predictions are estimates, "
        "not guarantees (18+)."
    )

    text_out = "\n".join(
        lines
    )

    bot.send_message(
        chat_id,
        text_out,
    )

    if daily:
        _daily_save(
            text_out
        )

    # ========================================================
    # TICKET IMAGE
    # ========================================================

    try:

        import ticket_image_lite

        rows = [
            {
                "time":
                    _time_text(
                        candidate,
                        today,
                    ),
                "league":
                    candidate[
                        "league"
                    ],
                "match": (
                    f"{candidate['home']} "
                    "vs "
                    f"{candidate['away']}"
                ),
                "pick":
                    candidate[
                        "label"
                    ],
                "odd":
                    candidate[
                        "odd"
                    ],
                "prob":
                    candidate[
                        "p"
                    ],
            }
            for candidate in chosen
        ]

        png = (
            ticket_image_lite
            .make_ticket_image(
                rows,
                (
                    "Daily 2 Odds"
                    if daily
                    else "Straight Win"
                    if straight
                    else (
                        f"{_fmt(target)} Odds"
                        if target
                        else (
                            f"{len(chosen)} Picks"
                        )
                    )
                ),
                req[
                    "label"
                ].capitalize(),
                total_odds,
                chance,
                code,
            )
        )

        _launcher_send_photo(
            chat_id,
            png,
            "",
        )

        if daily:
            _daily_save_image(
                png
            )

    except Exception as exc:

        print(
            f"Ticket picture failed: {exc}"
        )


# ============================================================
# DAILY CACHE
# ============================================================

_DAILY_LOCK = threading.Lock()

_DAILY_FILE = os.getenv(
    "DAILY_FILE",
    os.path.join(
        os.path.dirname(
            os.path.abspath(
                __file__
            )
        ),
        "daily_2odds.json",
    ),
)

_daily_cache = {}


def _daily_write():

    try:

        with open(
            _DAILY_FILE,
            "w",
            encoding="utf-8",
        ) as fh:

            json.dump(
                _daily_cache,
                fh,
            )

    except Exception as exc:

        print(
            f"Daily cache save failed: {exc}"
        )


def _daily_get():

    data = None

    try:

        with open(
            _DAILY_FILE,
            encoding="utf-8",
        ) as fh:

            data = json.load(
                fh
            )

    except Exception:

        data = (
            dict(
                _daily_cache
            )
            if _daily_cache
            else None
        )

    if not data:
        return None

    created = data.get(
        "created"
    )

    if (
        not created
        or not data.get(
            "text"
        )
    ):
        return None

    if (
        time.time()
        - created
        >= DAILY_SECONDS
    ):
        return None

    _daily_cache.clear()

    _daily_cache.update(
        data
    )

    return dict(
        data
    )


def _daily_save(
    text_out,
):

    _daily_cache.clear()

    _daily_cache.update(
        {
            "created":
                time.time(),
            "text":
                text_out,
        }
    )

    _daily_write()


def _daily_save_image(
    png,
):

    import base64

    if _daily_cache.get(
        "text"
    ):

        _daily_cache[
            "image"
        ] = base64.b64encode(
            png
        ).decode(
            "ascii"
        )

        _daily_write()


def _daily_left(
    cached,
):

    left = max(
        0,
        DAILY_SECONDS
        - (
            time.time()
            - cached[
                "created"
            ]
        ),
    )

    return (
        int(
            left // 3600
        ),
        int(
            (
                left
                % 3600
            )
            // 60
        ),
    )


def _daily_used(
    cached,
    person,
):

    users = (
        cached.get(
            "users"
        )
        or {}
    )

    return (
        users.get(
            str(person)
        )
        == cached.get(
            "created"
        )
    )


def _daily_mark(
    person,
    created,
):

    data = _daily_get()

    if (
        not data
        or data.get(
            "created"
        )
        != created
    ):
        return

    users = (
        _daily_cache.setdefault(
            "users",
            {},
        )
    )

    users[
        str(person)
    ] = created

    _daily_write()


def _daily_replay(
    chat_id,
    cached,
):

    import base64

    hours, minutes = (
        _daily_left(
            cached
        )
    )

    bot.send_message(
        chat_id,
        (
            cached["text"]
            + "\n\nð You can open "
            "Daily 2 odds once a day. "
            f"The next one unlocks in "
            f"{hours}h {minutes}m."
        ),
    )

    if cached.get(
        "image"
    ):

        try:

            _launcher_send_photo(
                chat_id,
                base64.b64decode(
                    cached["image"]
                ),
                "",
            )

        except Exception as exc:

            print(
                f"Daily image replay failed: {exc}"
            )


# ============================================================
# PUBLIC FLOW
# ============================================================

def flow(
    chat_id,
    text,
    search_days=None,
    target_override=None,
    count_override=None,
    **kwargs,
):

    if DAILY_RE.search(
        text or ""
    ):

        with _DAILY_LOCK:

            cached = _daily_get()

            if cached:

                if _daily_used(
                    cached,
                    chat_id,
                ):

                    hours, minutes = (
                        _daily_left(
                            cached
                        )
                    )

                    bot.send_message(
                        chat_id,
                        (
                            "ð You already opened "
                            "today's Daily 2 odds. "
                            f"The next one unlocks in "
                            f"{hours}h {minutes}m."
                        ),
                    )

                    return

                _daily_replay(
                    chat_id,
                    cached,
                )

                _daily_mark(
                    chat_id,
                    cached[
                        "created"
                    ],
                )

                return

            _flow_main(
                chat_id,
                text,
                search_days,
                target_override,
                count_override,
                daily=True,
            )

            fresh = _daily_get()

            if fresh:

                _daily_mark(
                    chat_id,
                    fresh[
                        "created"
                    ],
                )

            return

    return _flow_main(
        chat_id,
        text,
        search_days,
        target_override,
        count_override,
        **kwargs,
    )


# ============================================================
# HOOK INTO MAIN BOT
# ============================================================

_orig_flow = (
    bot.prediction_ticket_flow
)

bot.prediction_ticket_flow = flow

bot.MAX_DAYS_AHEAD = max(
    getattr(
        bot,
        "MAX_DAYS_AHEAD",
        2,
    ),
    3,
)
