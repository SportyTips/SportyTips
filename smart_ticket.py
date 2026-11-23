import html
import importlib
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

import main as bot
import sportybet_provider as sp


# ============================================================
# EMOJI ESCAPE CODES (keeps this file plain ASCII, so it can
# never be garbled by a wrong encoding again)
# ============================================================

E_TICKET = "\U0001F3AB"  # ticket
E_TIPS = "\U0001F3AF"  # target (pick / tip line)
E_CAL = "\U0001F4C5"  # calendar
E_CLOCK = "\U0001F552"  # clock
E_LEAGUE = "\U0001F3C6"  # trophy
E_BALL = "\u26BD"  # football
E_MONEY = "\U0001F4B0"  # money bag
E_CHAT = "\U0001F4AC"  # speech bubble
E_CHART = "\U0001F4CA"  # bar chart
E_WARN = "\u26A0\uFE0F"  # warning
E_INFO = "\u2139\uFE0F"  # info
E_SWAP = "\U0001F504"  # swap arrows
E_MIX = "\U0001F9E9"  # puzzle piece
E_CODE = "\U0001F4F2"  # phone with arrow
E_SCI = "\U0001F52C"  # microscope
E_LOCK = "\U0001F512"  # lock
E_FAIL = "\u274C"  # cross mark
DOT = "\u2022"  # bullet
LINE = "\u2501" * 12  # heavy line


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

NORMAL_MIN_LEG = float(os.getenv("NORMAL_MIN_LEG", "1.30"))

SKIP_LEAGUES = os.getenv("SKIP_LEAGUES", "1").strip().lower() in (
    "1", "true", "yes", "on",
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

# BIG ODDS: a target of BIG_ODDS_START or more ("5000 odds", "16000 odds") may use up to
# MAX_LEGS_RISKY legs instead of 30. 30 legs at about 1.28 each is only about 1,600 odds,
# which was the old ceiling. SportyBet itself limits how many games one ticket can hold,
# so keep MAX_LEGS_RISKY at or under that limit (change it on Render if needed).
MAX_LEGS_RISKY = int(os.getenv("MAX_LEGS_RISKY", "40"))
BIG_ODDS_START = float(os.getenv("BIG_ODDS_START", "500"))

# "I don't mind risky legs": weaker picks (down to this chance) are allowed.
RISKY_OK_MIN_P = float(os.getenv("RISKY_OK_MIN_P", "0.25"))

# When a ticket cannot be built, the message says why (set SHOW_BUILD_ERRORS=0 on Render to hide it).
SHOW_BUILD_ERRORS = os.getenv("SHOW_BUILD_ERRORS", "1").strip().lower() in ("1", "true", "yes", "on")

MAX_DETAIL_EVENTS = int(os.getenv("MAX_DETAIL_EVENTS", "100"))
DETAIL_WORKERS = int(os.getenv("DETAIL_WORKERS", "12"))
DETAIL_SECONDS = int(os.getenv("DETAIL_SECONDS", "40"))

# Big-odds tickets look at more games (and wait a little longer for their markets).
MAX_DETAIL_BIG = int(os.getenv("MAX_DETAIL_BIG", "160"))
DETAIL_SECONDS_BIG = int(os.getenv("DETAIL_SECONDS_BIG", "55"))

OVERSHOOT = 0.25

GROUP_LIMIT = int(os.getenv("GROUP_LIMIT", "200"))


# ============================================================
# MARKETS
# ============================================================

# Master switch: True = the old strict self-made rules are OFF (only the odds floor stays).
RULES_OFF = True
MIN_ODD_FLOOR = 1.12  # picks at 1.11 or lower are never used

# Double chance (Home or Draw / Draw or Away) is on again.
ALLOW_DOUBLE_CHANCE = True

# Football-data bonus used by selection optimizer.
DATA_BONUS = 0.12

USE_AI_REVIEW = os.getenv("USE_AI_REVIEW", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

WEB_SEARCHES = int(os.getenv("WEB_SEARCHES", "6"))

MAX_DROPS = 4

AI_TIMEOUT = int(os.getenv("AI_TIMEOUT", "50"))

STUDY_CACHE_SECONDS = int(os.getenv("STUDY_CACHE_SECONDS", "1800"))

_study_cache = {}

# Football data is ON by default, with SOFT fallback.
USE_FOOTBALL_DATA = os.getenv("USE_FOOTBALL_DATA", "1").strip().lower() in (
    "1", "true", "yes", "on",
)


# ============================================================
# STRAIGHT WIN SETTINGS
# ============================================================

STRAIGHT_MIN_P = float(os.getenv("STRAIGHT_MIN_P", "0.45"))
STRAIGHT_GOAL_MIN_P = float(os.getenv("STRAIGHT_GOAL_MIN_P", "0.45"))
STRAIGHT_1UP_MIN_P = float(os.getenv("STRAIGHT_1UP_MIN_P", "0.50"))
STRAIGHT_2UP_MIN_P = float(os.getenv("STRAIGHT_2UP_MIN_P", "0.65"))
STRAIGHT_2GOALS_MIN_P = float(os.getenv("STRAIGHT_2GOALS_MIN_P", "0.70"))

STRAIGHT_MIN_LEGS = 13
STRAIGHT_DEFAULT_LEGS = 15


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


NORMAL_MIN_ODD = _env_float("NORMAL_MIN_ODD", MIN_ODD_FLOOR)
RULE_AWAY_WIN_MIN_P = _env_float("RULE_AWAY_WIN_MIN_P", 0.48)
RULE_HOME_WIN_P = _env_float("RULE_HOME_WIN_P", 0.55)
HOME_WIN_ODD_MIN = _env_float("HOME_WIN_ODD_MIN", 1.50)
HOME_WIN_ODD_MAX = _env_float("HOME_WIN_ODD_MAX", 1.65)
RULE_WIN_MIN_P = _env_float("RULE_WIN_MIN_P", 0.42)
RULE_DRAW_MAX_P = _env_float("RULE_DRAW_MAX_P", 0.38)
RULE_UNDERDOG_OPP_P = _env_float("RULE_UNDERDOG_OPP_P", 0.65)
RULE_TEAMGOAL_MIN_P = _env_float("RULE_TEAMGOAL_MIN_P", 0.20)
RULE_TEAMGOAL2_MIN_P = _env_float("RULE_TEAMGOAL2_MIN_P", 0.45)
RULE_BTTS_O25_MAX = _env_float("RULE_BTTS_O25_MAX", 2.00)
RULE_CORNERS_O25_MAX = _env_float("RULE_CORNERS_O25_MAX", 2.20)
RULE_OVER15_O25_MAX = _env_float("RULE_OVER15_O25_MAX", 2.60)
RULE_MIN_MINUTES = _env_float("RULE_MIN_MINUTES", 0)
RULE_MARGIN_MIN = _env_float("RULE_MARGIN_MIN", 1.02)
RULE_MARGIN_MAX = _env_float("RULE_MARGIN_MAX", 1.20)

MAX_PER_LEAGUE = 0 if RULES_OFF else int(_env_float("MAX_PER_LEAGUE", 2))

RULE_DRAW_SKIP_GAME = os.getenv("RULE_DRAW_SKIP_GAME", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

SKIP_UNRELIABLE = (not RULES_OFF) and os.getenv("SKIP_UNRELIABLE", "1").strip().lower() in (
    "1", "true", "yes", "on",
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
DAILY_FROM_HOUR = 9  # Daily 2 odds uses games from 9am ...
DAILY_TO_HOUR = 20  # ... to 8pm (local time)


def _hour_text(hour):
    h = int(hour) % 24
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


DAILY_WINDOW_TEXT = f"{_hour_text(DAILY_FROM_HOUR)} to {_hour_text(DAILY_TO_HOUR)}"
DAILY_LOW = 1.90
DAILY_HIGH = 2.40
DAILY_MAX_ODD = 1.90

DAILY_MIN_P = float(os.getenv("DAILY_MIN_P", "0.0"))

DAILY_SECONDS = 24 * 60 * 60
DAILY_VERSION = 2  # raise this to throw away a saved Daily ticket built by older logic

DAILY_MIN_ODD = MIN_ODD_FLOOR
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
# WHAT THE PERSON TYPED: "over 2.5 only", "I don't mind risky legs", "16,000 odds"
# ============================================================

# "I don't mind risky legs", "risky is fine", "high risk", "bold" ...
RISK_OK_RE = re.compile(
    r"don'?t\s+mind\s+(?:the\s+)?risk"
    r"|do\s+not\s+mind\s+(?:the\s+)?risk"
    r"|\brisk(?:y)?\s+(?:legs?|picks?|games?|ok|fine)\b"
    r"|\bhigh[\s-]*risk\b"
    r"|\bbold\b"
    r"|\brisky\b",
    re.I,
)

# "over 2.5", "over 1.5 acca": a ticket made only of that goals line.
OVER_ONLY_RE = re.compile(
    r"\bover\s*([0-4]\.5)\b(?!\s*(?:team\s*)?(?:corners?|cards?|shots?))",
    re.I,
)


def _comma_free(text):
    """'16,000 odds' -> '16000 odds' (a comma inside a number is not a separator),
    and '7k odds' -> '7000 odds', '1.5k' -> '1500'."""

    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", str(text or ""))

    return re.sub(
        r"\b(\d+(?:\.\d+)?)\s?k\b",
        lambda m: str(int(round(float(m.group(1)) * 1000))),
        text,
        flags=re.I,
    )


def _parse_extras(text):
    """Returns (market_only, risk_ok).

    market_only is the goals line the ticket must be made of (2.5 for "over 2.5 only"),
    or None. risk_ok is True when the person says risky legs are fine.
    """

    text = _comma_free(text)

    market_only = None
    match = OVER_ONLY_RE.search(text)

    if match:
        try:
            market_only = float(match.group(1))
        except Exception:
            market_only = None

    return market_only, bool(RISK_OK_RE.search(text))


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
    "bb_win": 0.05,
    "bb_over": 0.02,
    "bb_hcp": 0.04,
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
    "bb_win": 0.40,
    "bb_over": 0.30,
    "bb_hcp": 0.30,
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
    "bb_win": "Basketball win",
    "bb_over": "Basketball over",
    "bb_hcp": "Basketball handicap",
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


def _compat(fn, args, extra=None, base=None):
    """Call fn(*args, **base, **extra).

    "extra" holds the newer options (big odds, basketball ...). If another module has
    replaced fn with an older version that does not know them, fn is called without them
    instead of crashing the whole ticket."""

    base = base or {}
    extra = extra or {}

    try:
        return fn(*args, **base, **extra)

    except TypeError as exc:

        text = str(exc)

        if extra and "argument" in text:
            print(
                f"NOTE: {getattr(fn, '__name__', 'a function')} does not know the new "
                f"options ({text[:100]}); using it without them"
            )
            return fn(*args, **base)

        raise


def _choose_target(groups, target, max_legs, caps):
    return _compat(choose_target, (groups, target), {"max_legs": max_legs, "caps": caps})


def _choose_count(groups, count, max_legs, caps):
    return _compat(choose_count, (groups, count), {"max_legs": max_legs, "caps": caps})


# ============================================================
# MARKET SAFETY
# ============================================================

def _candidate_is_allowed(kind, label=""):
    kind = str(kind or "").lower()
    label = str(label or "").lower()

    # User specifically does not want these.
    if kind in {"under", "yellow_cards", "cards"}:
        return False

    if "under " in label:
        return False

    if "yellow card" in label:
        return False

    if "cards" in label:
        return False

    # Only positive handicaps.
    if kind == "handicap":
        match = re.search(r"([+-]\d+(?:\.\d+)?)\s*handicap", label)

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

def _label_of(o):
    return str(o.get("desc") or o.get("name") or "").strip().lower()


def _hcp_line(spec):
    raw = spec.replace("hcp=", "")

    try:
        if ":" in raw:
            a, b = raw.split(":")
            return float(a) - float(b)

        return float(raw)

    except Exception:
        return None


def _daily_side(outcomes, o, three_way=False):
    side = _side_of(o, two_way=not three_way)

    if side:
        return side

    if not three_way and len(outcomes) == 2:
        return "home" if outcomes[0] is o else "away"

    return None


def event_candidates(event, markets):
    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")

    out = []

    def add(kind, label, odd, p, key, **extra):
        if not _candidate_is_allowed(kind, label):
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

    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

    if h and d and a:
        inv = [1 / h, 1 / d, 1 / a]
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
            (sp.M_1X2, "", sp.OUT_1X2["home"]),
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
        k1 = (sp.M_DC, "", sp.OUT_DC["1x"])
        k2 = (sp.M_DC, "", sp.OUT_DC["x2"])

        add(
            "dc",
            f"{home} or Draw",
            sp.find_odds(markets, k1),
            (ph + pd if ph is not None else None),
            k1,
            side="home",
        )

        add(
            "dc",
            f"Draw or {away}",
            sp.find_odds(markets, k2),
            (pd + pa if pa is not None else None),
            k2,
            side="away",
        )

    # --------------------------------------------------------
    # TOTAL GOALS
    # --------------------------------------------------------

    for market in markets or []:

        if str(market.get("id")) != sp.M_TOTAL:
            continue

        spec = market.get("specifier") or ""

        if not spec.startswith("total="):
            continue

        line = _f(spec.replace("total=", ""))

        if line is None:
            continue

        if (line * 2) % 1 != 0:
            continue

        over = None
        under = None

        for outcome in market.get("outcomes", []):

            if outcome.get("isActive") is False:
                continue

            oid = str(outcome.get("id"))

            if oid == sp.OUT_TOTAL["over"]:
                over = _f(outcome.get("odds"))

            elif oid == sp.OUT_TOTAL["under"]:
                under = _f(outcome.get("odds"))

        mid = str(market.get("id"))

        add(
            "over15" if line == 1.5 else "over",
            f"Over {line:g} goals",
            over,
            _two_way(over, under),
            (mid, spec, sp.OUT_TOTAL["over"]),
            line=line,
        )

    # --------------------------------------------------------
    # BTTS
    # --------------------------------------------------------

    yes = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    no = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["no"]))

    add(
        "btts",
        "Both teams to score",
        yes,
        _two_way(yes, no),
        (sp.M_BTTS, "", sp.OUT_BTTS["yes"]),
        btts="yes",
    )

    # --------------------------------------------------------
    # OTHER MARKETS
    # --------------------------------------------------------

    for market in markets or []:

        mid = str(market.get("id"))

        if mid in (sp.M_1X2, sp.M_DC, sp.M_TOTAL, sp.M_BTTS):
            continue

        name = (
            f"{market.get('desc') or ''} "
            f"{market.get('name') or ''}"
        ).lower()

        spec = market.get("specifier") or ""

        outcomes = [
            o
            for o in market.get("outcomes", [])
            if o.get("isActive") is not False
        ]

        # ----------------------------------------------------
        # 1UP / 2UP
        # ----------------------------------------------------

        up_match = re.search(r"1x2\W+([12])\s*-?\s*up\b", name)

        if up_match:

            number = up_match.group(1)

            for outcome in outcomes:

                side = _side_of(outcome)
                odd = _f(outcome.get("odds"))

                if side not in ("home", "away") or not odd:
                    continue

                team = home if side == "home" else away
                market_probability = ph if side == "home" else pa

                add(
                    "up",
                    f"{team} to win ({number}UP)",
                    odd,
                    _single(odd),
                    (mid, spec, str(outcome.get("id"))),
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
                for x in ("both", "1st", "2nd", "first", "second")
            )
        ):

            for side, team in (("home", home), ("away", away)):

                for outcome in outcomes:

                    label = str(
                        outcome.get("desc")
                        or outcome.get("name")
                        or ""
                    ).strip().lower()

                    odd = _f(outcome.get("odds"))

                    if not odd:
                        continue

                    if side in label or (side in name and label == "yes"):

                        add(
                            "either_half",
                            f"{team} to win either half",
                            odd,
                            _single(odd),
                            (mid, spec, str(outcome.get("id"))),
                            side=side,
                            market_side_p=(ph if side == "home" else pa),
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
                    "1st", "2nd", "first", "second", "half",
                    "home", "away", "team", "race", "handicap",
                    "odd", "even", "1x2", "exact", "range",
                )
            )
        ):

            line = _f(spec.replace("total=", ""))

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

                oid = str(outcome.get("id"))
                odd = _f(outcome.get("odds"))

                if label.startswith("over") or oid == sp.OUT_TOTAL["over"]:
                    over = odd
                    over_id = oid

            add(
                "corners",
                f"Over {line:g} corners",
                over,
                _single(over),
                (mid, spec, over_id),
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
                for x in ("1st", "2nd", "half", "3-way", "3 way", "three")
            )
        ):

            line = _hcp_line(spec)

            if not line:
                continue

            odds = [_f(o.get("odds")) for o in outcomes]

            for outcome, odd, other in zip(outcomes, odds, odds[::-1]):

                side = _side_of(outcome, two_way=True)

                if not side or not odd:
                    continue

                if side == "home" and line > 0:
                    team = home

                elif side == "away" and line < 0:
                    team = away

                else:
                    continue

                label = f"{team} +{abs(line):g} handicap"

                add(
                    "handicap",
                    label,
                    odd,
                    _two_way(odd, other),
                    (mid, spec, str(outcome.get("id"))),
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

            line = _f(spec.replace("total=", ""))

            if line is None:
                continue

            for outcome in outcomes:

                label = str(
                    outcome.get("desc")
                    or outcome.get("name")
                    or ""
                ).strip()

                low = label.lower()

                if not low.startswith("over"):
                    continue

                odd = _f(outcome.get("odds"))

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
                    f"{team} Over {line:g} team goals",
                    odd,
                    _single(odd),
                    (mid, spec, str(outcome.get("id"))),
                    side=side,
                    line=line,
                )

            continue

    return out


# ============================================================
# DAILY CANDIDATES
# ============================================================

def daily_candidates(event, markets):
    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")

    out = []

    def add(kind, label, odd, p, key, low=None, high=None, **extra):

        # Daily mode follows the same market restrictions.
        if not _candidate_is_allowed(kind, label):
            return

        odd = _safe_float(odd)
        p = _safe_float(p)

        if not odd or not p:
            return

        if odd < (low or DAILY_MIN_ODD):
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

    for c in event_candidates(event, markets):

        if (
            c["kind"] in (
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

        mid = str(market.get("id"))
        spec = market.get("specifier") or ""

        outcomes = [
            o
            for o in market.get("outcomes", [])
            if o.get("isActive") is not False
        ]

        if not outcomes:
            continue

        # HOME/AWAY 12 - disabled.
        if mid == "10":
            continue

        # ----------------------------------------------------
        # TOTAL GOALS
        # ----------------------------------------------------

        if mid == "18" and spec.startswith("total="):

            line = _f(spec.replace("total=", ""))

            if line is None or (line * 2) % 1 != 0:
                continue

            for o in outcomes:

                if (
                    _label_of(o).startswith("over")
                    or str(o.get("id")) == sp.OUT_TOTAL["over"]
                ):

                    odd = _f(o.get("odds"))

                    add(
                        "over",
                        f"Over {line:g} goals",
                        odd,
                        _single(odd),
                        (mid, spec, str(o.get("id"))),
                        line=line,
                    )

        # ----------------------------------------------------
        # BTTS
        # ----------------------------------------------------

        elif mid == "29":

            for o in outcomes:

                lab = _label_of(o)
                odd = _f(o.get("odds"))

                if lab.startswith("yes"):
                    name = "Both teams to score"
                else:
                    continue

                add(
                    "btts",
                    name,
                    odd,
                    _single(odd),
                    (mid, spec, str(o.get("id"))),
                    low=DAILY_BTTS_MIN,
                )

        # ----------------------------------------------------
        # TEAM GOALS
        # ----------------------------------------------------

        elif mid in ("19", "20") and spec.startswith("total="):

            line = _f(spec.replace("total=", ""))

            if line not in (0.5, 1.5):
                continue

            team = home if mid == "19" else away

            for o in outcomes:

                if _label_of(o).startswith("over"):

                    odd = _f(o.get("odds"))

                    add(
                        "team_goals",
                        f"{team} Over {line:g} team goals",
                        odd,
                        _single(odd),
                        (mid, spec, str(o.get("id"))),
                        line=line,
                    )

        # ----------------------------------------------------
        # CORNERS
        # ----------------------------------------------------

        elif mid == "166" and spec.startswith("total="):

            line = _f(spec.replace("total=", ""))

            if line not in (6.5, 7.5):
                continue

            for o in outcomes:

                if _label_of(o).startswith("over"):

                    odd = _f(o.get("odds"))

                    add(
                        "corners",
                        f"Over {line:g} corners",
                        odd,
                        _single(odd),
                        (mid, spec, str(o.get("id"))),
                        high=DAILY_CORNER_MAX,
                        line=line,
                    )

        # ----------------------------------------------------
        # FIRST-HALF CORNERS
        # ----------------------------------------------------

        elif mid == "177" and spec.startswith("total="):

            line = _f(spec.replace("total=", ""))

            if line != 3.5:
                continue

            for o in outcomes:

                if _label_of(o).startswith("over"):

                    odd = _f(o.get("odds"))

                    add(
                        "corners",
                        "Over 3.5 corners (1st half)",
                        odd,
                        _single(odd),
                        (mid, spec, str(o.get("id"))),
                        high=DAILY_CORNER_MAX,
                        line=line,
                    )

        elif mid == "64":
            continue

        # ----------------------------------------------------
        # POSITIVE HANDICAP
        # ----------------------------------------------------

        elif mid in ("65", "66") and spec.startswith("hcp="):

            line = _hcp_line(spec)

            if not line:
                continue

            three_way = mid == "65"

            for o in outcomes:

                side = _daily_side(outcomes, o, three_way)

                if side == "home" and line > 0:
                    team = home

                elif side == "away" and line < 0:
                    team = away

                else:
                    continue

                odd = _f(o.get("odds"))

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

                    p = _two_way(odd, other)

                add(
                    "handicap",
                    f"{team} +{abs(line):g} handicap",
                    odd,
                    p,
                    (mid, spec, str(o.get("id"))),
                    side=side,
                    handicap=abs(line),
                )

    return out


def _pick_for(cands, kind, **attrs):

    for c in cands:

        if c.get("kind") != kind:
            continue

        if all(c.get(k) == v for k, v in attrs.items()):
            return c

    return None


def _team_over(markets, home, away, side, line):

    want = "19" if side == "home" else "20"
    team = home if side == "home" else away

    for market in markets or []:

        if str(market.get("id")) != want:
            continue

        spec = market.get("specifier") or ""

        if not spec.startswith("total="):
            continue

        if _f(spec.replace("total=", "")) != line:
            continue

        for o in market.get("outcomes", []):

            if o.get("isActive") is False:
                continue

            if not _label_of(o).startswith("over"):
                continue

            odd = _f(o.get("odds"))

            if not odd or odd <= 1:
                continue

            goals = 1 if line == 0.5 else 2

            return {
                "kind": "team_goals",
                "label": f"{team} to score {goals}+ goals (Over {line:g})",
                "odd": odd,
                "p": _single(odd),
                "key": (want, spec, str(o.get("id"))),
                "side": side,
                "line": line,
            }

    return None


# ============================================================
# STRAIGHT WIN
# ============================================================

def straight_candidates(event, markets):

    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")

    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

    if not (h and d and a):
        return []

    inv = [1 / h, 1 / d, 1 / a]
    total = sum(inv)

    if not total:
        return []

    chances = {
        "home": inv[0] / total,
        "away": inv[2] / total,
    }

    base = event_candidates(event, markets)

    out = []

    for side in ("home", "away"):

        w = chances[side]

        if w < STRAIGHT_GOAL_MIN_P:
            continue

        mine = [c for c in base if c.get("side") == side]

        def up(level, mine=mine):
            return _pick_for(mine, "up", up_level=level)

        def goals(line, mine=mine, side=side):
            return (
                _team_over(markets, home, away, side, line)
                or _pick_for(mine, "team_goals", line=line)
            )

        if w >= STRAIGHT_2UP_MIN_P:

            pick = _pick_for(mine, "win")

            if not pick and w >= STRAIGHT_2GOALS_MIN_P:
                pick = goals(1.5) or up(2) or up(1)

            if not pick:
                pick = up(2) or up(1)

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

def _event_ok(event, league_name):

    if SKIP_UNRELIABLE:

        text = " ".join(
            [
                str(league_name or ""),
                str(event.get("homeTeamName") or ""),
                str(event.get("awayTeamName") or ""),
            ]
        )

        if _UNRELIABLE_RE.search(text):
            return False

    start_time = event.get("estimateStartTime") or 0

    try:

        if (
            RULE_MIN_MINUTES > 0
            and start_time
            and (start_time / 1000 - time.time()) < RULE_MIN_MINUTES * 60
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

        h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
        d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
        a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

        if h and d and a:

            inv = [1 / h, 1 / d, 1 / a]
            total = sum(inv)

            if total:

                ctx["margin"] = total
                ctx["ph"] = inv[0] / total
                ctx["pd"] = inv[1] / total
                ctx["pa"] = inv[2] / total

        ctx["o25"] = sp.find_odds(
            markets,
            (sp.M_TOTAL, "total=2.5", sp.OUT_TOTAL["over"]),
        )

    except Exception:
        pass

    return ctx


def _passes_rules(c, ctx, relax=0):

    if RULES_OFF:
        return True

    kind = c.get("kind")
    side = c.get("side")

    ph = ctx["ph"]
    pd = ctx["pd"]
    pa = ctx["pa"]
    o25 = ctx["o25"]

    own = ph if side == "home" else pa if side == "away" else None
    opp = pa if side == "home" else ph if side == "away" else None

    if kind in ("up", "either_half", "win"):

        need = RULE_AWAY_WIN_MIN_P if side == "away" else RULE_WIN_MIN_P

        if relax >= 2:
            need = RULE_WIN_MIN_P

        if own is None or own < need:
            return False

        draw_max = RULE_DRAW_MAX_P + (0.05 if relax >= 2 else 0)

        if pd is not None and pd > draw_max:
            return False

    elif kind == "handicap":

        if opp is not None and opp >= RULE_UNDERDOG_OPP_P:
            return False

    elif kind == "team_goals":

        line = c.get("line") or 0

        need = RULE_TEAMGOAL_MIN_P if line <= 0.5 else RULE_TEAMGOAL2_MIN_P

        if own is not None and own < need:
            return False

    elif kind == "btts":

        if "not" not in str(c.get("label", "")).lower():

            if o25 and o25 > RULE_BTTS_O25_MAX:
                return False

    elif kind == "corners":

        if o25 and o25 > RULE_CORNERS_O25_MAX:
            return False

    elif kind == "over15":

        if o25 and o25 > RULE_OVER15_O25_MAX:
            return False

    return True


# ============================================================
# FOOTBALL DATA STUDY
# ============================================================

BTTS_MIN_SCORE_RATE = _env_float("BTTS_MIN_SCORE_RATE", 0.60)
BTTS_MIN_GAMES = int(_env_float("BTTS_MIN_GAMES", 3))


def _btts_supported(facts):
    """GG (both teams to score) only when BOTH teams really score in this setting.

    The home team's HOME games and the away team's AWAY games are looked at. A team
    that scored in fewer than 60% of them (for example 0 goals in its last 5 home
    games) blocks the GG pick. No team stats at all also means no GG, because it
    could not be checked.
    """

    if not facts:
        return False

    venue = facts.get("venue") or {}
    recent = facts.get("recent") or {}

    for side in ("home", "away"):

        data = venue.get(side) or {}

        if (data.get("played") or 0) < BTTS_MIN_GAMES:
            data = recent.get(side) or {}

        played = data.get("played") or 0

        if played < BTTS_MIN_GAMES:
            return False

        scored_in = data.get("scored_in")

        if scored_in is None:
            return False  # facts from older code: cannot prove it, so no GG

        if scored_in / played < BTTS_MIN_SCORE_RATE:
            return False

    return True


UP_MIN_SCORE_RATE = _env_float("UP_MIN_SCORE_RATE", 0.60)      # picked team scores in 60%+ of its games
UP_MIN_CONCEDE_RATE = _env_float("UP_MIN_CONCEDE_RATE", 0.50)  # opponent concedes in 50%+ of its games
UP2_MIN_AVG_GOALS = _env_float("UP2_MIN_AVG_GOALS", 1.40)      # 2UP: needs a team that scores plenty
UP_NEED_STATS = os.getenv("UP_NEED_STATS", "0").strip().lower() in ("1", "true", "yes", "on")


def _is_up(candidate):

    return candidate.get("kind") == "up" and candidate.get("side") in ("home", "away")


def _up_supported(candidate):
    """1UP / 2UP only for a team that really gets goals, against a team that lets them in.

    A team can only go a goal up if it scores, so the picked team's scoring record in
    this setting (home team at home, away team away) is checked, and the opponent must
    concede in half of its games. 2UP also needs an average of 1.4 goals or more.

    Without team stats the pick is kept (the SportyBet odds are all there is), unless
    UP_NEED_STATS=1 is set on Render, which then drops every 1UP / 2UP that has no stats.
    """

    facts = candidate.get("facts")

    if not facts:
        return not UP_NEED_STATS

    side = candidate.get("side")
    opponent = "away" if side == "home" else "home"
    venue = facts.get("venue") or {}
    recent = facts.get("recent") or {}

    def sample(team_side):

        data = venue.get(team_side) or {}

        if (data.get("played") or 0) < BTTS_MIN_GAMES:
            data = recent.get(team_side) or {}

        return data

    mine = sample(side)
    theirs = sample(opponent)

    played = mine.get("played") or 0

    if played < BTTS_MIN_GAMES or mine.get("scored_in") is None:
        return not UP_NEED_STATS

    if mine["scored_in"] / played < UP_MIN_SCORE_RATE:
        return False

    if int(candidate.get("up_level") or 1) >= 2:
        average = mine.get("avg_scored")

        if average is not None and average < UP2_MIN_AVG_GOALS:
            return False

    their_played = theirs.get("played") or 0

    if their_played >= BTTS_MIN_GAMES and theirs.get("conceded_in") is not None:

        if theirs["conceded_in"] / their_played < UP_MIN_CONCEDE_RATE:
            return False

    return True


def _is_gg_yes(candidate):

    return (
        candidate.get("kind") == "btts"
        and "not" not in str(candidate.get("label", "")).lower()
    )


def study(groups, notify=None):
    """
    Football data is studied BEFORE the evidence gate.

    SportyBet odds determine which market exists.
    Football data determines whether the match is actually supported.
    When football data is missing we fall back to SportyBet odds.
    """

    import football_data as fd

    # --- DEBUG PRINT ADDED HERE ---
    print(f"DEBUG STUDY: USE_FOOTBALL_DATA={USE_FOOTBALL_DATA}, ENRICH_MAX={fd.ENRICH_MAX}, groups={len(groups)}")
    # ------------------------------

    if not USE_FOOTBALL_DATA or fd.ENRICH_MAX <= 0 or not groups:
        return 0

    # ENRICH_MAX is a maximum, NOT a quota.
    top = groups[:fd.ENRICH_MAX]

    if notify:
        try:
            notify(len(top))
        except Exception:
            pass

    studied = 0
    started = time.time()

    for group in top:

        if time.time() - started > fd.ENRICH_SECONDS:
            break

        try:

            event = group[0]["event"]
            event_id = event.get("eventId")

            hit = _study_cache.get(event_id)

            facts = None

            if hit and (time.time() - hit[0] < STUDY_CACHE_SECONDS):
                facts = hit[1]

            else:

                fixture = fd.find_fixture(event)

                if not fixture:
                    continue

                facts = fd.get_facts(fixture)

                if facts:
                    _study_cache[event_id] = (time.time(), facts)

            if not facts:
                continue

            studied += 1

            for candidate in group:

                try:
                    football_probability = fd.adjusted_p(candidate, facts)
                except Exception:
                    football_probability = 0.0

                candidate["football_p"] = max(
                    0.0,
                    min(0.99, float(football_probability)),
                )

                # From this point forward p means football-evidence probability.
                candidate["p"] = candidate["football_p"]
                candidate["facts"] = facts
                candidate["has_data"] = True

        except Exception as exc:
            print(f"Football study failed: {exc}")

    return studied


# ============================================================
# BASKETBALL (NCAA, NBA ...)
# ============================================================
#
# Basketball games come from SportyBet's own game list (sport id sr:sport:2) and are
# read through the same provider as football. Every market is found by its NAME on the
# game page (winner, total points, positive handicap, all "incl. overtime"), the same way
# the 1UP / 2UP markets are, so no market number has to be right for it to work.
#
# There are no football stats for basketball, so the chance of a pick comes from the
# SportyBet prices with the bookmaker margin removed. Only Over totals and positive
# handicaps are used (never Under), the same rule as football.

BASKETBALL_SPORT_ID = "sr:sport:2"

# Only used to ask SportyBet for these markets in the game list. If a number is wrong the
# markets are still read from each game page.
BASKETBALL_MARKET_IDS = os.getenv("BASKETBALL_MARKET_IDS", "219,223,225")
BB_EVENT_PAGES = int(os.getenv("BB_EVENT_PAGES", "8"))
BB_CACHE_SECONDS = int(os.getenv("BB_CACHE_SECONDS", "300"))

# A "mixed" ticket has at least this many basketball legs (fewer if there is no room).
BB_MIXED_LEGS = int(os.getenv("BB_MIXED_LEGS", "3"))

BB_LEAGUES = {
    "ncaa": re.compile(r"ncaa|college", re.I),
    "nba": re.compile(r"\bnba\b", re.I),
    "euroleague": re.compile(r"euro\s*league", re.I),
}

BB_WORD_RE = re.compile(
    r"\b(?:basket\s*ball|ncaab?|nba|euro\s*league|college\s+basketball)\b",
    re.I,
)

BB_MIXED_RE = re.compile(r"\bmix(?:ed)?\b|\bfootball\b|\bsoccer\b", re.I)

_BB_PERIOD = (
    "quarter", "half", "1st", "2nd", "3rd", "4th",
    "first", "second", "third", "fourth", "period",
)

_bb_cache = {"time": 0.0, "events": []}
_BB_LOCK = threading.Lock()


def _is_basketball(event):

    if event.get("_sport") == "basketball":
        return True

    sport = event.get("sport") or {}

    return (
        str(sport.get("id") or "") == BASKETBALL_SPORT_ID
        or str(sport.get("name") or "").strip().lower() == "basketball"
    )


def _parse_sport(text):
    """Returns (sport, league): "football" | "basketball" | "mixed", and "ncaa" / "nba" / None.

    "ncaa 100 odds" and "basketball 100 odds"  -> basketball only
    "safe 10 odds mixed with basketball"        -> football and basketball together
    """

    low = _comma_free(text).lower()

    if not BB_WORD_RE.search(low):
        return "football", None

    named = []

    if re.search(r"\bncaab?\b|\bcollege\b", low):
        named.append("ncaa")

    if re.search(r"\bnba\b", low):
        named.append("nba")

    if re.search(r"euro\s*league", low):
        named.append("euroleague")

    # "ncaa and nba": both leagues, so no single-league filter
    league = named[0] if len(named) == 1 else None

    mixed = bool(BB_MIXED_RE.search(low))

    return ("mixed" if mixed else "basketball"), league


def _bb_events(provider):
    """Every upcoming basketball game on SportyBet (kept for a few minutes)."""

    with _BB_LOCK:

        if _bb_cache["events"] and time.time() - _bb_cache["time"] < BB_CACHE_SECONDS:
            return _bb_cache["events"]

    walk = getattr(sp, "_walk_events", None)
    virtual = getattr(sp, "is_virtual", lambda e: False)

    if walk is None or not hasattr(provider, "_request"):
        raise RuntimeError("This SportyBet provider cannot load basketball games.")

    events = []
    seen = set()

    for page in range(1, BB_EVENT_PAGES + 1):

        result = provider._request(
            sp.UPCOMING_PATH,
            {
                "sportId": BASKETBALL_SPORT_ID,
                "marketId": BASKETBALL_MARKET_IDS,
                "pageSize": 100,
                "pageNum": page,
                "todayGames": "false",
            },
        )

        found = [
            e
            for e in walk(result.get("data"))
            if e.get("eventId") not in seen
        ]

        if not found:
            break

        for event in found:

            seen.add(event.get("eventId"))

            if virtual(event):
                continue

            event["_sport"] = "basketball"
            events.append(event)

    with _BB_LOCK:
        _bb_cache["events"] = events
        _bb_cache["time"] = time.time()

    return events


_BB_DEBUG = {"games": 0, "with_markets": 0, "with_picks": 0, "league_skipped": 0, "names": []}

_BB_LABEL_BAD = re.compile(r"\bunder\b|\bcards?\b", re.I)


def _bb_side(outcome, home, away):
    """home / away from an outcome's own label (Home, Away, 1, 2 or the team name)."""

    label = _label_of(outcome)
    oid = str(outcome.get("id"))

    if label.startswith("home") or label == "1" or (len(home) > 2 and label == home.lower()):
        return "home"

    if label.startswith("away") or label == "2" or (len(away) > 2 and label == away.lower()):
        return "away"

    if oid == "4":  # SportyBet's basketball winner ids are 4 (home) and 5 (away)
        return "home"

    if oid == "5":
        return "away"

    return None


def basketball_candidates(event, markets):
    """Winner, Over total points and positive handicap (all incl. overtime)."""

    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")

    out = []

    def add(kind, label, odd, p, key, **extra):

        # basketball picks do not depend on any other module's list of allowed pick types
        if _BB_LABEL_BAD.search(str(label or "")):
            return

        odd = _safe_float(odd)
        p = _safe_float(p)

        if not odd or odd <= 1 or not p or p <= 0:
            return

        item = {"kind": kind, "label": label, "odd": odd, "p": p, "key": key}
        item.update(extra)
        out.append(item)

    for market in markets or []:

        mid = str(market.get("id"))
        spec = market.get("specifier") or ""

        name = (
            f"{market.get('desc') or ''} "
            f"{market.get('name') or ''}"
        ).lower()

        outcomes = [
            o
            for o in market.get("outcomes", [])
            if o.get("isActive") is not False
        ]

        # remember a few market names, so a problem can be seen in the message
        if len(_BB_DEBUG["names"]) < 8:

            sample = f"{mid}: {name.strip()}" + (f" [{spec}]" if spec else "") + f" ({len(outcomes)})"

            if sample not in _BB_DEBUG["names"]:
                _BB_DEBUG["names"].append(sample)

        if len(outcomes) != 2:
            continue

        # Only the whole game, never one quarter or one half.
        if any(word in name for word in _BB_PERIOD):
            continue

        odds = [_f(o.get("odds")) for o in outcomes]

        # ---- winner of the game (overtime included) ----
        if (
            not spec
            and not any(w in name for w in ("odd", "even", "exact", "handicap", "total", "margin", "range", "most"))
        ):

            sides = [_bb_side(o, home, away) for o in outcomes]
            named_winner = any(w in name for w in ("winner", "moneyline", "money line", "to win", "match result"))

            if len(set(sides)) == 2 and None not in sides:
                pass  # both outcomes are clearly home / away

            elif named_winner:
                sides = ["home", "away"]  # a market called "winner": first is home, second is away

            else:
                sides = None

            if sides:

                for o, odd, other, side in zip(outcomes, odds, odds[::-1], sides):

                    team = home if side == "home" else away

                    add(
                        "bb_win",
                        f"{team} to win",
                        odd,
                        _two_way(odd, other),
                        (mid, spec, str(o.get("id"))),
                        side=side,
                    )

                continue

        # ---- Over total points (never Under) ----
        if (
            spec.startswith("total=")
            and any(w in name for w in ("total", "over/under", "o/u"))
            and not any(w in name for w in ("home", "away", "team", "odd", "even", "exact"))
        ):

            line = _f(spec.replace("total=", ""))

            if line is None or (line * 2) % 1 != 0:
                continue

            over = under = None
            over_id = None

            for o, odd in zip(outcomes, odds):

                label = _label_of(o)

                if label.startswith("over"):
                    over, over_id = odd, str(o.get("id"))

                elif label.startswith("under"):
                    under = odd

            add(
                "bb_over",
                f"Over {line:g} points",
                over,
                _two_way(over, under),
                (mid, spec, over_id),
                line=line,
            )

            continue

        # ---- positive handicap only ----
        if spec.startswith("hcp=") and "handicap" in name:

            line = _hcp_line(spec)

            if not line:
                continue

            for index, (o, odd, other) in enumerate(zip(outcomes, odds, odds[::-1])):

                side = _bb_side(o, home, away) or ("home" if index == 0 else "away")

                if side == "home" and line > 0:
                    team = home

                elif side == "away" and line < 0:
                    team = away

                else:
                    continue

                add(
                    "bb_hcp",
                    f"{team} +{abs(line):g} handicap",
                    odd,
                    _two_way(odd, other),
                    (mid, spec, str(o.get("id"))),
                    side=side,
                    handicap=abs(line),
                )

    return out


def _split_sport(groups):

    football, basketball = [], []

    for group in groups:

        if group and group[0].get("sport") == "basketball":
            basketball.append(group)
        else:
            football.append(group)

    return football, basketball


def _force_basketball(groups, limit_product=None, need=None):
    """A 'mixed' ticket must really contain basketball.

    The strongest basketball pick of the best games is taken first, then the rest of
    the ticket is built around them. Returns (forced picks, the other games, their odds).
    """

    need = BB_MIXED_LEGS if need is None else need

    _, basketball = _split_sport(groups)

    ranked = sorted(
        basketball,
        key=lambda g: max(c.get("p", 0) for c in g),
        reverse=True,
    )

    forced, used = [], 1.0

    for group in ranked:

        if len(forced) >= need:
            break

        best = max(group, key=lambda c: c.get("p", 0))

        if limit_product and used * best["odd"] > limit_product:
            continue

        forced.append(best)
        used *= best["odd"]

    ids = {c["event_id"] for c in forced}
    rest = [g for g in groups if g[0]["event_id"] not in ids]

    return forced, rest, used


# ============================================================
# FAVOURITE / DETAIL ORDER
# ============================================================

def favourite_strength(event):

    markets = event.get("markets") or []

    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

    if not (h and d and a):
        return 0.0

    try:

        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)

        if not total:
            return 0.0

        return max(inv[0], inv[2]) / total

    except Exception:
        return 0.0


def _detail_football(events, limit):

    if len(events) <= limit:
        return list(events)

    ranked = sorted(events, key=favourite_strength, reverse=True)

    selected = []
    seen = set()

    priority_count = min(limit // 2, len(ranked))

    for event in ranked[:priority_count]:

        eid = event.get("eventId")

        if eid in seen:
            continue

        selected.append(event)
        seen.add(eid)

    remaining = limit - len(selected)

    if remaining > 0:

        step = max(1, len(events) // remaining)

        for index in range(0, len(events), step):

            event = events[index]
            eid = event.get("eventId")

            if eid in seen:
                continue

            selected.append(event)
            seen.add(eid)

            if len(selected) >= limit:
                break

    return selected[:limit]


def _detail_order(events, limit=None):

    limit = limit or MAX_DETAIL_EVENTS

    basketball = [e for e in events if _is_basketball(e)]

    if not basketball:
        return _detail_football(events, limit)

    # Basketball games are few: they all get their markets loaded first.
    basketball = basketball[: max(limit // 2, 1)]
    football = [e for e in events if not _is_basketball(e)]
    room = max(limit - len(basketball), 0)

    return basketball + (_detail_football(football, room) if room else [])


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
    market_only=None,
    risk_ok=False,
    detail_limit=None,
    detail_seconds=None,
    sport="football",
    league_hint=None,
):
    """
    sport: "football", "basketball" or "mixed". league_hint: "ncaa", "nba" ... (basketball).
    market_only: a goals line (2.5) - only "Over 2.5 goals" picks are kept.
    risk_ok: the person does not mind risky legs, so weaker picks are allowed.
    detail_limit / detail_seconds: big-odds tickets look at more games.
    """

    events = []

    if sport in ("football", "mixed"):
        events.extend(provider.get_upcoming(start, end))

    if sport in ("basketball", "mixed"):

        try:

            for game in _bb_events(provider):

                ms = game.get("estimateStartTime")

                if ms and start.timestamp() <= ms / 1000 < end.timestamp():
                    events.append(game)

        except Exception as exc:
            print(f"Basketball games could not be loaded: {type(exc).__name__}: {exc}")

    if sport in ("basketball", "mixed"):
        _BB_DEBUG.update(games=0, with_markets=0, with_picks=0, league_skipped=0, names=[])

    if not events:
        return [], 0, 0, 0

    wanted = _compat(_detail_order, (events,), {"limit": detail_limit} if detail_limit else None)

    details = {}

    if wanted:

        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:

            futures = {}

            for event in wanted:

                try:

                    event_id = event["eventId"]

                    futures[
                        pool.submit(
                            provider._event_markets_cached,
                            event_id,
                        )
                    ] = event

                except Exception:
                    continue

            try:

                for future in as_completed(
                    futures,
                    timeout=(detail_seconds or DETAIL_SECONDS),
                ):

                    event = futures[future]

                    try:
                        details[event["eventId"]] = future.result()
                    except Exception:
                        pass

            except FutureTimeout:
                pass

    min_p = MIN_LEG_PROB.get(risk, MIN_LEG_PROB["normal"]) - min_p_shift
    max_odd = MAX_LEG_ODDS.get(risk, MAX_LEG_ODDS["normal"])

    post_min_p = min_p

    if straight:

        min_p = 0.30
        post_min_p = STRAIGHT_MIN_P
        max_odd = 2.60

    if daily:

        min_p = 0.30
        post_min_p = DAILY_MIN_P
        max_odd = DAILY_MAX_ODD

    # "I don't mind risky legs": weaker picks are allowed.
    if risk_ok and not straight and not daily:
        post_min_p = min(post_min_p, RISKY_OK_MIN_P)

    groups = []

    # --------------------------------------------------------
    # We DO NOT filter candidates by odds-derived p here.
    # First build valid market candidates, then football_data
    # studies the match, then football probability is used
    # (or SportyBet fallback).
    # --------------------------------------------------------

    for event in events:

        markets = (
            details.get(event["eventId"])
            or event.get("markets")
            or []
        )

        try:
            fixture = sp.sporty_fixture(event)
        except Exception:
            fixture = {"league": {"name": "Football"}}

        league_name = str((fixture.get("league", {}) or {}).get("name", ""))

        if SKIP_LEAGUES and SKIP_LEAGUE_RE.search(league_name):
            continue

        if not _event_ok(event, league_name):
            continue

        is_bb = _is_basketball(event)

        # "ncaa 100 odds": only the games of that basketball league.
        if is_bb and league_hint:

            pattern = BB_LEAGUES.get(league_hint)
            country = str((fixture.get("league", {}) or {}).get("country", ""))

            if pattern and not pattern.search(f"{league_name} {country}"):
                _BB_DEBUG["league_skipped"] += 1
                continue

        ctx = _match_context(markets)

        margin = ctx.get("margin")

        if (
            not RULES_OFF
            and margin is not None
            and not (RULE_MARGIN_MIN <= margin <= RULE_MARGIN_MAX)
        ):
            continue

        if (
            RULE_DRAW_SKIP_GAME
            and ctx["pd"] is not None
            and ctx["pd"] > RULE_DRAW_MAX_P
        ):
            continue

        if is_bb and not daily and not straight:

            _BB_DEBUG["games"] += 1

            if markets:
                _BB_DEBUG["with_markets"] += 1

            source = basketball_candidates(event, markets)

            if source:
                _BB_DEBUG["with_picks"] += 1

        elif daily:
            source = daily_candidates(event, markets)

        elif straight:
            source = straight_candidates(event, markets)

        else:
            source = event_candidates(event, markets)

        # "over 2.5 only" / "over 1.5 acca": keep only that goals line (football legs).
        if market_only is not None and not daily and not straight and not is_bb:

            source = [
                c
                for c in source
                if c.get("kind") in ("over15", "over")
                and c.get("line") == market_only
            ]

        kept = []
        seen = set()

        for candidate in source:

            if is_bb:
                if _BB_LABEL_BAD.search(str(candidate["label"])):
                    continue

            elif not _candidate_is_allowed(candidate["kind"], candidate["label"]):
                continue

            # Rule checks that do not require football data.
            if not straight and not _passes_rules(candidate, ctx, relax):
                continue

            if not ((MIN_ODD_FLOOR if is_bb else floor) <= candidate["odd"] <= max_odd):
                continue

            key = (event["eventId"], candidate["key"])

            if key in exclude:
                continue

            if candidate["label"] in seen:
                continue

            seen.add(candidate["label"])

            start_time = event.get("estimateStartTime") or 0

            try:
                kickoff = datetime.fromtimestamp(
                    start_time / 1000,
                    tz=timezone.utc,
                )
            except Exception:
                kickoff = datetime.now(timezone.utc)

            league_data = fixture.get("league", {}) or {}

            # p is still only a temporary market value here.
            # It is replaced by football_data if available.
            candidate.update(
                {
                    "event": event,
                    "event_id": event["eventId"],
                    "home": event.get("homeTeamName", "Home"),
                    "away": event.get("awayTeamName", "Away"),
                    "kickoff": kickoff,
                    "league": league_data.get("name", "Football"),
                    "has_data": False,
                    "facts": None,
                    "sport": "basketball" if is_bb else "football",
                    "ctx": ctx,
                    "market_p": candidate.get("p", 0.0),
                    "win_p": max(ctx["ph"] or 0, ctx["pa"] or 0),
                }
            )

            kept.append(candidate)

        if kept:
            groups.append(kept)

    # --------------------------------------------------------
    # Study football BEFORE sorting/filtering by probability.
    # --------------------------------------------------------

    if USE_FOOTBALL_DATA:
        studied = study(
            [g for g in groups if not (g and g[0].get("sport") == "basketball")],
            notify,
        )

    else:
        # Compatibility mode only.
        studied = 0

        for group in groups:
            for candidate in group:

                candidate["has_data"] = True
                candidate["facts"] = None
                candidate["football_p"] = candidate.get("market_p", 0.0)
                candidate["p"] = candidate["football_p"]

    # --------------------------------------------------------
    # SOFT evidence gate - keep picks even without football data
    # --------------------------------------------------------

    evidence_groups = []

    for group in groups:

        valid = []

        for candidate in group:

            # Soft fallback: when no football data, use SportyBet market probability.
            if USE_FOOTBALL_DATA and not candidate.get("has_data"):
                candidate["p"] = candidate.get("market_p", 0.45)
                # keep has_data = False so we know it is a fallback

            if candidate.get("p", 0) < post_min_p and not candidate.get("bypass"):
                continue

            # GG needs proof that both teams score. No stats = no GG.
            if _is_gg_yes(candidate) and not _btts_supported(candidate.get("facts")):
                continue

            # 1UP / 2UP needs a team that scores against a team that concedes.
            if _is_up(candidate) and not _up_supported(candidate):
                continue

            valid.append(candidate)

        if valid:
            evidence_groups.append(valid)

    # Sort AFTER football study / fallback.
    evidence_groups.sort(
        key=lambda group: max(c.get("p", 0) for c in group),
        reverse=True,
    )

    evidence_groups = evidence_groups[:GROUP_LIMIT]

    return (
        evidence_groups,
        len(events),
        len(details),
        studied,
    )


# ============================================================
# DYNAMIC PROGRAMMING
# ============================================================

def _dp(groups, target, penalty=None, S=None):
    """Cheapest set of picks (one per game) whose odds multiply to the target.

    penalty: the cost of each extra leg. A higher value makes the search reach the
    target with fewer, higher-odds legs.
    S: how finely odds are measured. Big targets use a coarser scale so the search
    stays fast.
    """

    if not groups or not target:
        return None

    S = S or (200 if target <= 300 else 80)
    leg_penalty = LEG_PENALTY if penalty is None else penalty

    try:

        tw = math.ceil(math.log(target) * S)

        wm = max(
            tw,
            int(math.log(target * (1 + OVERSHOOT)) * S),
        )

    except Exception:
        return None

    INF = float("inf")

    dp = [INF] * (wm + 1)
    dp[0] = 0.0

    choices = []

    for group in groups:

        new = dp[:]
        choice = [None] * (wm + 1)

        for oi, candidate in enumerate(group):

            odd = candidate.get("odd")
            probability = candidate.get("p")

            if not odd or odd <= 1 or not probability or probability <= 0:
                continue

            try:

                weight = round(math.log(odd) * S)

                cost = (
                    -math.log(probability)
                    - KIND_BONUS.get(candidate.get("kind"), 0.0)
                    + leg_penalty
                    - (DATA_BONUS if candidate.get("has_data") else 0.0)
                )

            except Exception:
                continue

            if weight <= 0 or weight > wm:
                continue

            for x in range(0, wm - weight + 1):

                base = dp[x]

                if base == INF:
                    continue

                value = base + cost

                if value < new[x + weight]:

                    new[x + weight] = value
                    choice[x + weight] = (oi, x)

        dp = new
        choices.append(choice)

    best = None

    for x in range(tw, wm + 1):

        if dp[x] < INF and (best is None or dp[x] < dp[best]):
            best = x

    if best is None:
        return None

    picked = []

    x = best

    for gi in range(len(groups) - 1, -1, -1):

        step = choices[gi][x]

        if step is None:
            continue

        index, previous = step

        picked.append(groups[gi][index])

        x = previous

    picked.reverse()

    # NOTE: the old code cut the result to 30 legs here, which silently threw away
    # odds. The leg limit is now handled in _fit_legs, which keeps the target.
    return picked


def _product(picks):

    total = 1.0

    for candidate in picks:
        total *= candidate["odd"]

    return total


def _dp_exact(groups, target, penalty=None):

    if not groups:
        return None

    aim = target
    best = None

    for _ in range(6):

        picks = _compat(_dp, (groups, aim), {"penalty": penalty} if penalty is not None else None)

        if not picks:
            break

        actual = _product(picks)

        if actual >= target and (best is None or actual < _product(best)):
            best = picks

        if target <= actual <= target * (1 + OVERSHOOT + 0.02):
            return picks

        if actual <= 0:
            break

        aim = max(target, aim * target / actual * 1.004)

    return best


def _fit_legs(groups, target, max_legs):
    """Reach the target with at most max_legs legs.

    First the cheapest set of legs is tried. If it needs too many legs, the cost of
    each leg is raised, so the search reaches for higher-odds legs and uses fewer.
    """

    penalty = LEG_PENALTY

    for _ in range(4):

        picks = _compat(_dp_exact, (groups, target), {"penalty": penalty})

        if not picks:
            # Even every game together cannot reach the target: more legs will not help.
            return None

        if len(picks) <= max_legs:
            return picks

        penalty *= 3.0

    return None


def _best_effort(groups, max_legs):
    """The target cannot be reached: the highest-odds pick of the most games."""

    best = []

    for group in groups:

        usable = [c for c in group if c.get("odd", 0) > 1]

        if usable:
            best.append(max(usable, key=lambda c: c["odd"]))

    best.sort(key=lambda c: c["odd"], reverse=True)

    return best[:max_legs]


def _prune(groups, legs, scale):

    drop = set()

    for kind, share in KIND_CAP.items():

        cap = max(1, math.ceil(share * legs * scale))

        ranked = sorted(
            (
                candidate
                for group in groups
                for candidate in group
                if candidate["kind"] == kind
            ),
            key=lambda candidate: candidate.get("p", 0),
            reverse=True,
        )

        drop.update(id(candidate) for candidate in ranked[cap:])

    pruned = []

    for group in groups:

        kept = [c for c in group if id(c) not in drop]

        if kept:
            pruned.append(kept)

    return pruned


def _kind_counts(chosen):

    counts = {}

    for candidate in chosen:

        kind = candidate["kind"]
        counts[kind] = counts.get(kind, 0) + 1

    return counts


def choose_target(groups, target, max_legs=None, caps=True):
    """caps=False (big odds, or 'over 2.5 only') skips the per-market share limits,
    which used to keep only the strongest (lowest-odds) picks of each market."""

    max_legs = max_legs or MAX_LEGS

    if not groups or not target:
        return [], False

    all_weights = sorted(
        math.log(candidate["odd"])
        for group in groups
        for candidate in group
        if candidate.get("odd", 0) > 1
    )

    if not all_weights:
        return [], False

    if not caps:

        chosen = _fit_legs(groups, target, max_legs)

        if chosen:
            return chosen, True

        return [], False

    median_weight = all_weights[len(all_weights) // 2]

    legs = max(
        3,
        min(
            max_legs,
            round(math.log(target) / max(median_weight, 0.05)),
        ),
    )

    scale = 1.0

    for _ in range(5):

        chosen = None

        for _ in range(4):

            pruned = _prune(groups, legs, scale)

            if not pruned:
                scale *= 1.6
                continue

            chosen = _fit_legs(pruned, target, max_legs)

            if chosen:
                break

            scale *= 1.6

        if not chosen:
            break

        counts = _kind_counts(chosen)

        valid_mix = True

        for kind, share in KIND_CAP.items():

            allowed = max(1, math.ceil(share * len(chosen) * scale))

            if counts.get(kind, 0) > allowed:
                valid_mix = False
                break

        if valid_mix:
            return chosen, True

        legs = len(chosen)

    return [], False


def choose_count(groups, count, max_legs=None, caps=True):

    if not groups or count <= 0:
        return [], False

    count = min(int(count), max_legs or MAX_LEGS)

    # Soft: no longer require has_data
    ranked = sorted(
        (candidate for group in groups for candidate in group),
        key=lambda candidate: (
            candidate.get("p", 0)
            + 0.25 * candidate.get("win_p", 0)
            + KIND_BONUS.get(candidate.get("kind"), 0)
            + (DATA_BONUS if candidate.get("has_data") else 0)
        ),
        reverse=True,
    )

    chosen = []

    for scale in ((1.0, 1.6, 2.5, 4.0) if caps else (1.0,)):

        chosen = []
        used_events = set()
        counts = {}

        for candidate in ranked:

            event_id = candidate["event_id"]

            if event_id in used_events:
                continue

            kind = candidate["kind"]

            cap = max(
                1,
                math.ceil(KIND_CAP.get(kind, 0.30) * count * scale),
            )

            if caps and counts.get(kind, 0) >= cap:
                continue

            chosen.append(candidate)
            used_events.add(event_id)
            counts[kind] = counts.get(kind, 0) + 1

            if len(chosen) >= count:
                return chosen, True

    return chosen, len(chosen) >= count


def choose_straight(groups, target, count):

    best = []

    for group in groups:

        usable = [c for c in group if c.get("p", 0) > 0]

        if usable:
            best.append(max(usable, key=lambda c: c.get("p", 0)))

    best.sort(key=lambda c: c.get("p", 0), reverse=True)

    if not best:
        return [], False

    if count:

        count = min(int(count), MAX_LEGS)

        return best[:count], len(best) >= count

    if target:

        chosen = []
        total = 1.0

        for candidate in best[:MAX_LEGS]:

            chosen.append(candidate)
            total *= candidate["odd"]

            if total >= target:
                return chosen, True

        return chosen, False

    return best[:STRAIGHT_DEFAULT_LEGS], True


def choose_daily(groups):

    import itertools

    best = []

    for group in groups:

        usable = [c for c in group if c.get("p", 0) > 0]

        if usable:
            best.append(max(usable, key=lambda c: c.get("p", 0)))

    best.sort(key=lambda c: c.get("p", 0), reverse=True)

    pool = best[:25]

    for low, high in ((2.00, 2.20), (2.00, 2.50)):  # never below 2.00

        top = None
        top_chance = 0.0

        for size in (2, 3):

            for combo in itertools.combinations(pool, size):

                total = 1.0
                chance = 1.0

                for candidate in combo:

                    total *= candidate["odd"]
                    chance *= candidate["p"]

                if not (low <= total <= high):
                    continue

                if chance > top_chance:

                    top = combo
                    top_chance = chance

        if top:
            print(f"Daily 2 odds: {len(top)} picks chosen (window {low:.2f}-{high:.2f})")
            return list(top), True

    print("Daily 2 odds: no combination of 2.00 or more found")
    return [], False


# ============================================================
# AI REVIEW
# ============================================================

def _ai_json(prompt, system):

    if not ANTHROPIC_API_KEY:
        return None

    for use_tools in (True, False):

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
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": WEB_SEARCHES,
                }
            ]

        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )

        try:

            wait = AI_TIMEOUT if use_tools else 25

            with urlopen(request, timeout=wait) as response:
                data = json.loads(response.read().decode("utf-8"))

        except Exception as exc:

            print(f"AI review failed: {exc}")
            continue

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

        parsed = _parse_ai_json(text)

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


def ai_review(chosen, local_now):

    lines = []

    for i, candidate in enumerate(chosen):

        when = candidate["kickoff"].astimezone(bot.LOCAL_TZ).strftime("%a %H:%M")

        lines.append(
            f"{i}. "
            f"{candidate['home']} vs {candidate['away']} "
            f"({candidate['league']}) "
            f"kickoff {when} | "
            f"pick: {candidate['label']} | "
            f"SportyBet price {candidate['odd']:.2f}"
        )

    prompt = (
        "Today is "
        f"{local_now.strftime('%A %d %B %Y')} "
        "(Nigeria time).\n\n"
        + "\n".join(lines)
    )

    data = _ai_json(prompt, REVIEW_SYSTEM)

    reasons = {}

    if not data:
        return reasons

    items = data.get("picks", [])

    if not isinstance(items, list):
        return reasons

    for item in items:

        try:
            index = int(item.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue

        if not (0 <= index < len(chosen)):
            continue

        reason = str(item.get("reason", "")).strip()[:240]

        flag = (
            "drop"
            if (str(item.get("flag", "")).lower() == "drop" and reason)
            else "keep"
        )

        if reason:

            candidate = chosen[index]

            reasons[(candidate["event_id"], candidate["key"])] = (reason, flag)

    return reasons


# ============================================================
# FOOTBALL REASON
# ============================================================

def _reason_for(candidate):

    facts = candidate.get("facts")

    if facts:

        try:

            import football_data as fd

            return fd.reason_for(
                candidate,
                facts,
                candidate["home"],
                candidate["away"],
            )

        except Exception:
            pass

    # Do NOT describe SportyBet odds as football evidence.
    p = round(candidate.get("p", 0) * 100)

    side_p = candidate.get("market_side_p")
    side = candidate.get("side")

    if side_p and side in ("home", "away"):

        team = candidate["home"] if side == "home" else candidate["away"]

        return (
            f"SportyBet market support for {team} is about "
            f"{p}% for this market."
        )

    return f"SportyBet market analysis supports this selection at about {p}%."


# ============================================================
# RELAXATION
# ============================================================

def _relax_note(level):

    if level == 1:
        return (
            "I loosened one selection rule to fill your ticket: up to "
            f"{MAX_PER_LEAGUE + 1} picks per league."
        )

    if level >= 2:
        return (
            "I loosened selection rules to find enough football-supported "
            f"picks: up to {MAX_PER_LEAGUE + 2} picks per league."
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
    opts=None,
):
    """opts: {"market_only": 2.5 or None, "risk_ok": True/False}"""

    best = None
    best_level = 0

    # With RULES_OFF the loosening levels change nothing, so one pass is enough
    # (the extra passes only made a failed request take three times as long).
    levels = 1 if RULES_OFF else 3

    for level in range(levels):

        built = _compat(
            _build_once,
            (provider, req, target, count, risk, exclude, notify, straight, daily, level),
            {"opts": opts},
        )

        chosen = built["chosen"]

        if best is None or len(chosen) > len(best["chosen"]):

            best = built
            best_level = level

        short = (
            not chosen
            or (count and len(chosen) < count)
            or (target and not built["reached"])
        )

        if not short:

            best = built
            best_level = level
            break

    if best_level and best["chosen"]:

        note = _relax_note(best_level)

        best["note"] = (
            (best["note"] + " " if best["note"] else "")
            + note
        )

    return best


def _fmt(value):

    try:
        value = float(value)
    except Exception:
        return str(value)

    if value.is_integer():
        return str(int(value))

    return f"{value:g}"


def _time_text(candidate, today):

    local = candidate["kickoff"].astimezone(bot.LOCAL_TZ)

    text = local.strftime("%I:%M %p").lstrip("0")

    if local.date() == today:
        return text

    return local.strftime("%a ") + text


def _confidence(p):

    if p >= 0.80:
        return "\U0001F7E2"  # green circle

    if p >= 0.70:
        return "\U0001F7E1"  # yellow circle

    return "\U0001F7E0"  # orange circle


def _excess(chosen, relax=0):

    drop = []

    cap = MAX_PER_LEAGUE + relax if MAX_PER_LEAGUE > 0 else 0

    if cap > 0:

        by = {}

        for c in chosen:
            by.setdefault(c.get("league"), []).append(c)

        for items in by.values():

            if len(items) > cap:

                items.sort(key=lambda c: c.get("p", 0), reverse=True)

                drop.extend(items[cap:])

    gone = {id(c) for c in drop}

    seen = set()

    for c in sorted(chosen, key=lambda c: c.get("p", 0), reverse=True):

        if id(c) in gone:
            continue

        teams = {
            str(c.get("home", "")).lower(),
            str(c.get("away", "")).lower(),
        }

        if teams & seen:

            drop.append(c)
            gone.add(id(c))

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
    opts=None,
):

    opts = opts or {}

    # "over 2.5 only": every leg must be that goals line.
    market_only = None if (straight or daily) else opts.get("market_only")

    # "I don't mind risky legs" (or the word risky): weaker picks are allowed.
    risk_ok = (
        (bool(opts.get("risk_ok")) or risk == "risky")
        and not (straight or daily)
    )

    # football (default), basketball, or both ("mixed")
    sport = "football" if (straight or daily) else (opts.get("sport") or "football")
    league_hint = None if (straight or daily) else opts.get("league")

    # BIG ODDS (500 and up): up to MAX_LEGS_RISKY legs, a wider pool of games, and no
    # per-market share limits (those limits kept only the lowest-odds picks).
    big = bool(target and target >= BIG_ODDS_START) and not (straight or daily)
    max_legs = MAX_LEGS_RISKY if big else MAX_LEGS
    use_caps = not (market_only is not None or big)

    floor = NORMAL_MIN_LEG

    if risk == "risky":
        floor = max(floor, 1.35)

    floor = max(floor, NORMAL_MIN_ODD)

    if market_only is not None:
        # Over 1.5 is often priced 1.20 to 1.40: do not throw those away.
        floor = MIN_ODD_FLOOR

    if sport == "basketball":
        # Basketball favourites are often priced 1.12 to 1.30.
        floor = MIN_ODD_FLOOR

    if straight:
        floor = 1.05

    if daily:
        floor = DAILY_MIN_ODD

    extra_days = 0
    note = ""

    chosen = []
    reached = False
    total_events = 0
    detailed = 0
    studied = 0
    usable = 0  # games that had at least one pick that fits the request

    # "tonight" (9pm to 5am) sets exact_window: never borrow games from other days.
    max_extra = 0 if req.get("exact_window") else 4

    while extra_days <= max_extra:

        end = req["end"] + timedelta(days=extra_days)

        gather_extra = {
            "market_only": market_only,
            "risk_ok": risk_ok,
            "detail_limit": (MAX_DETAIL_BIG if (big or sport != "football") else None),
            "detail_seconds": (DETAIL_SECONDS_BIG if (big or sport != "football") else None),
            "sport": sport,
            "league_hint": league_hint,
        }

        # only the options that are really switched on are passed on
        gather_extra = {
            k: v for k, v in gather_extra.items() if v not in (None, False, "football")
        }

        (
            groups,
            total_events,
            detailed,
            studied,
        ) = _compat(
            gather,
            (
                provider,
                req["start"],
                end,
                risk,
                floor,
                exclude,
                (0.05 if (target and target <= 20) else 0.0),
                notify,
            ),
            gather_extra,
            {"straight": straight, "daily": daily, "relax": relax},
        )

        usable = len(groups)

        if not groups:

            extra_days += 1
            continue

        def pick(gs):

            if daily:
                return choose_daily(gs)

            if straight:
                return choose_straight(gs, target, count)

            if target:

                picks, ok = None, False

                # "mixed": the strongest basketball picks go in first, the rest is built around them.
                if sport == "mixed":

                    forced, rest, used = _force_basketball(gs, target * 0.6)

                    if forced:

                        left = target / used

                        if left <= 1.15:
                            picks, ok = list(forced), used >= target

                        else:

                            more, ok = _choose_target(
                                rest,
                                left,
                                max(1, max_legs - len(forced)),
                                use_caps,
                            )

                            if more:
                                picks = list(forced) + more

                if not picks:
                    picks, ok = _choose_target(gs, target, max_legs, use_caps)

                if not picks and (
                    req.get("exact_window")
                    or (big and extra_days >= max_extra)
                ):
                    # Could not reach it: give the best there is (shown with a
                    # "could not reach" warning) instead of nothing.
                    if big:
                        picks = _best_effort(gs, max_legs)
                    else:
                        # Small night slate: the best strong picks there are.
                        picks, _ = _choose_count(gs, max_legs, max_legs, use_caps)

                    ok = False

                return picks, ok

            if sport == "mixed":

                wanted = int(count)
                need = min(BB_MIXED_LEGS, max(1, wanted // 3))
                forced, rest, _used = _force_basketball(gs, None, need)

                if forced:

                    left = wanted - len(forced)

                    if left <= 0:
                        return list(forced)[:wanted], True

                    more, ok = _choose_count(rest, left, max_legs, use_caps)

                    return list(forced) + more, ok

            return _choose_count(gs, count, max_legs, use_caps)

        chosen, reached = pick(groups)

        capped = groups

        for _ in range(8):

            drop = _excess(chosen, relax)

            if not drop:
                break

            banned = {c["event_id"] for c in drop}

            capped = [g for g in capped if g[0]["event_id"] not in banned]

            if not capped:
                break

            trial, ok = pick(capped)

            if not trial:
                break

            chosen = trial
            reached = ok

        too_few = (
            straight
            and not target
            and len(chosen) < (count or STRAIGHT_MIN_LEGS)
            and extra_days < 2
        )

        if chosen and not too_few:
            break

        extra_days += 1

    else:

        # (the number of games looked at is kept: the message to the person uses it)
        chosen = []
        reached = False

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
        "usable": usable,
    }


# ============================================================
# IMAGE
# ============================================================

def _launcher_send_photo(chat_id, png, caption=""):

    try:

        launcher = importlib.import_module(
            os.getenv("LAUNCHER_MODULE", "launcher")
        )

        send = getattr(launcher, "send_photo", None)

        if send:
            send(chat_id, png, caption)

    except Exception as exc:
        print(f"Ticket picture failed: {exc}")


def _what_text(sport_mode, market_only):
    """What kind of picks the person asked for, in words."""

    if market_only is not None:
        return f"Over {market_only:g} goals picks"

    if sport_mode == "basketball":
        return "basketball picks"

    if sport_mode == "mixed":
        return "football and basketball picks"

    return "picks"


def _searched_text(built, req, sport_mode, market_only):
    """What was searched on SportyBet and what was found there."""

    looked = int(built.get("events") or 0)
    usable = int(built.get("usable") or 0)
    what = _what_text(sport_mode, market_only)

    label = str(req.get("label") or "that window")

    if label.startswith("next"):
        label = "the " + label

    if looked <= 0:

        games = "basketball games" if sport_mode == "basketball" else "games"

        return f"I looked through SportyBet for {label} but could not find any {games}."

    games = f"{looked} game{'s' if looked != 1 else ''}"

    if usable >= looked:
        return f"I looked through SportyBet for {label} and found {games} with {what} that fit your request."

    return (
        f"I looked through SportyBet for {label} and found {games}, but only {usable} "
        f"{'has' if usable == 1 else 'have'} {what} that fit your request."
    )


def _shortfall_text(built, req, target, count, sport_mode, market_only, risk_ok, picks, total_odds):
    """Why the ticket is short: what was searched on SportyBet and what was found."""

    found = _searched_text(built, req, sport_mode, market_only)

    if target:
        result = (
            f"The best ticket I could build is {total_odds:.0f} odds "
            f"({picks} pick{'s' if picks != 1 else ''}), not {_fmt(target)}."
        )
    elif count:
        result = f"That is only {picks} of the {int(count)} picks you asked for."
    else:
        result = ""

    advice = (
        "There are not enough matches on SportyBet for that right now. "
        "Try a longer window (for example the next 3 days), "
        + ("or lower odds." if risk_ok else "lower odds, or say you don't mind risky legs.")
    )

    return " ".join(part for part in (found, result, advice) if part)


def _bb_debug_text():
    """A short technical line, shown when a basketball ticket could not be built."""

    import sys as _sys

    parts = [
        f"basketball games checked: {_BB_DEBUG['games']}",
        f"with markets: {_BB_DEBUG['with_markets']}",
        f"with usable picks: {_BB_DEBUG['with_picks']}",
    ]

    if _BB_DEBUG["league_skipped"]:
        parts.append(f"skipped by league name: {_BB_DEBUG['league_skipped']}")

    if _BB_DEBUG["names"]:
        parts.append("markets seen: " + "; ".join(_BB_DEBUG["names"][:6]))

    if "upgrades" in _sys.modules and not getattr(_sys.modules.get("ticket_extras"), "LOADED", False):
        parts.append("ticket_extras.py is NOT loaded, so upgrades.py may block basketball: add its lines to app.py")

    return "Details: " + " | ".join(parts)


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

    # "16,000 odds" must be read as 16000.
    text = _comma_free(text)

    provider = getattr(bot, "SPORTYBET_PROVIDER", None)

    if provider is None:

        bot.send_message(
            chat_id,
            f"{E_FAIL} SportyBet mode is off on this server.",
        )

        return

    try:

        req = bot.parse_request(text)

    except Exception as exc:

        print(f"parse_request failed: {exc}")

        bot.send_message(
            chat_id,
            f"{E_FAIL} I couldn't understand that request. Try: 10 odds today",
        )

        return

    if daily:

        # Daily 2 odds: only games from 9am to 8pm, never games from another day.
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc.astimezone(bot.LOCAL_TZ)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        earliest = now_utc + timedelta(hours=getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1))

        day_start = midnight + timedelta(hours=DAILY_FROM_HOUR)
        day_end = midnight + timedelta(hours=DAILY_TO_HOUR)
        day_name = "today"

        if day_end <= earliest:
            # Today's window is over: use tomorrow's.
            day_start += timedelta(days=1)
            day_end += timedelta(days=1)
            day_name = "tomorrow"

        req["start"] = max(earliest, day_start)
        req["end"] = day_end
        req["daily_day"] = day_start.strftime("%Y-%m-%d")
        req["label"] = f"{day_name} ({DAILY_WINDOW_TEXT})"
        req["exact_window"] = True

    if search_days and req.get("label") == "next 24 hours":

        try:

            days = int(search_days)

            if days > 1:

                req["end"] = req["start"] + timedelta(days=days)
                req["label"] = f"next {days} days"

        except Exception:
            pass

    target = target_override or req.get("target_odds")
    count = count_override or req.get("picks")
    risk = req.get("risk") or "normal"

    straight = bool(STRAIGHT_RE.search(text or ""))

    if (
        straight
        and re.search(r"\blong\b", text or "", re.I)
        and req.get("label") == "next 24 hours"
    ):

        req["end"] = req["start"] + timedelta(days=5)
        req["label"] = "next 5 days"

    if not target:

        match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:total\s*)?odds?\b",
            text or "",
            re.I,
        )

        if match:

            try:

                value = float(match.group(1))

                if value >= 1.5:

                    target = value
                    count = None

            except Exception:
                pass

    if daily:

        target = DAILY_TARGET
        count = None
        risk = "safe"

    # "over 2.5 only" and "I don't mind risky legs" (not used by Daily / Straight win)
    market_only, risk_ok = (None, False) if (daily or straight) else _parse_extras(text)

    if risk_ok:
        risk = "risky"

    # "ncaa 100 odds", "basketball 100 odds", "safe 10 odds mixed with basketball"
    sport_mode, league_hint = ("football", None) if (daily or straight) else _parse_sport(text)

    if straight and not target and not count:

        count = STRAIGHT_DEFAULT_LEGS

        if req.get("label") == "next 24 hours":

            req["end"] = req["start"] + timedelta(days=3)
            req["label"] = "next 3 days"

    elif not target and not count:

        # "over 1.5 acca today" with no size: 10 legs (say "20 picks" or "5000 odds" for more)
        count = 10 if market_only is not None else 5

    big_run = bool(target and target >= BIG_ODDS_START) and not (daily or straight)

    opts = {
        "market_only": market_only,
        "risk_ok": risk_ok,
        "sport": sport_mode,
        "league": league_hint,
    }

    try:

        built = _compat(
            build_ticket,
            (provider, req, target, count, risk),
            {"opts": opts},
            {"notify": None, "straight": straight, "daily": daily},
        )

    except Exception as exc:

        print(f"Smart ticket failed: {type(exc).__name__}: {exc}")

        import traceback

        traceback.print_exc()

        why = ""

        if SHOW_BUILD_ERRORS:
            why = "\n\nDetails: " + html.escape(f"{type(exc).__name__}: {str(exc)[:200]}")

        bot.send_message(
            chat_id,
            f"{E_FAIL} Something went wrong while building the ticket. "
            f"Please try again in a minute.{why}",
        )

        return

    chosen = built["chosen"]

    if not chosen and risk != "risky" and not straight and not daily:

        try:

            retry = _compat(
                build_ticket,
                (provider, req, target, count, "risky"),
                {"opts": opts},
                {"notify": None},
            )

            if retry["chosen"]:

                built = retry
                chosen = retry["chosen"]

                built["note"] = (
                    (built["note"] + " " if built["note"] else "")
                    + "I widened the market limits to find more picks."
                )

        except Exception as exc:
            print(f"Wider retry failed: {exc}")

    local_now = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ)

    if not chosen and daily:

        bot.send_message(
            chat_id,
            f"{E_FAIL} I couldn't find a Daily 2 odds combination right now. "
            "Try again later.",
        )

        return

    if not chosen and straight:

        bot.send_message(
            chat_id,
            f"{E_FAIL} I couldn't find enough straight win picks right now. "
            "Try again later or ask for a straight win long ticket.",
        )

        return

    if not chosen:

        if sport_mode != "football":

            bot.send_message(
                chat_id,
                f"{E_FAIL} I couldn't build a basketball ticket for that request. "
                + html.escape(_searched_text(built, req, sport_mode, market_only))
                + " Try a longer window or lower odds."
                + (("\n\n" + html.escape(_bb_debug_text())) if SHOW_BUILD_ERRORS else ""),
            )

            return

        if market_only is not None:

            bot.send_message(
                chat_id,
                f"{E_FAIL} I couldn't build that ticket. "
                + html.escape(_searched_text(built, req, sport_mode, market_only))
                + " Try a longer window (for example the next 3 days), fewer picks, "
                "or say you don't mind risky legs.",
            )

            return

        bot.send_message(
            chat_id,
            f"{E_FAIL} I couldn't build a ticket for that request. "
            + html.escape(_searched_text(built, req, sport_mode, market_only))
            + " Try a different time window or lower odds.",
        )

        return

    # ========================================================
    # AI REVIEW
    # ========================================================

    reasons = {}
    swapped = []
    reviewed = False

    # Big-odds tickets (many legs, risky on purpose) skip the AI review: it would
    # drop legs and rebuild the whole ticket.
    if (
        USE_AI_REVIEW
        and ANTHROPIC_API_KEY
        and not straight
        and not big_run
        and sport_mode == "football"  # the review is written for football
    ):

        try:

            for round_no in range(2):

                fresh = [
                    candidate
                    for candidate in chosen
                    if (candidate["event_id"], candidate["key"]) not in reasons
                ]

                if fresh:
                    reasons.update(ai_review(fresh, local_now))

                if not reasons:
                    break

                reviewed = True

                if round_no == 1:
                    break

                drops = [
                    candidate
                    for candidate in chosen
                    if reasons.get(
                        (candidate["event_id"], candidate["key"]),
                        ("", "keep"),
                    )[1] == "drop"
                ][:MAX_DROPS]

                if not drops:
                    break

                exclude = frozenset(
                    (candidate["event_id"], candidate["key"])
                    for candidate in drops
                )

                rebuilt = _compat(
                    build_ticket,
                    (provider, req, target, count, risk),
                    {"opts": opts},
                    {"exclude": exclude, "straight": straight, "daily": daily},
                )

                if not rebuilt["chosen"]:
                    break

                swapped = [
                    (
                        candidate,
                        reasons[(candidate["event_id"], candidate["key"])][0],
                    )
                    for candidate in drops
                    if (candidate["event_id"], candidate["key"]) in reasons
                ]

                built = rebuilt
                chosen = rebuilt["chosen"]

        except Exception as exc:

            print(f"AI review step failed: {exc}")

    # ========================================================
    # FINAL SORT
    # ========================================================

    chosen.sort(key=lambda c: c["kickoff"])

    total_odds = 1.0
    chance = 1.0

    for candidate in chosen:

        total_odds *= candidate["odd"]
        chance *= candidate["p"]

        candidate["reason"] = reasons.get(
            (candidate["event_id"], candidate["key"]),
            (_reason_for(candidate), "keep"),
        )[0]

    # ========================================================
    # REAL SPORTYBET BOOKING CODE
    # ========================================================

    code = None

    try:

        code = provider.create_booking_code(
            [
                (
                    candidate["event"],
                    {"resolved_key": candidate["key"]},
                )
                for candidate in chosen
            ]
        )

    except Exception as exc:

        print(f"Booking code failed: {exc}")

    # ========================================================
    # TELEGRAM OUTPUT
    # ========================================================

    today = local_now.date()

    if daily:
        title = "DAILY 2 ODDS"

    elif straight:
        title = f"STRAIGHT WIN {DOT} {len(chosen)} PICKS"

    elif target:
        title = f"{_fmt(target)} ODDS"

    else:
        title = f"{len(chosen)} PICKS"

    if market_only is not None:
        title = f"OVER {market_only:g} {DOT} {title}"

    if sport_mode == "basketball":
        title = f"{(league_hint or 'BASKETBALL').upper()} {DOT} {title}"

    elif sport_mode == "mixed":
        title = f"FOOTBALL + BASKETBALL {DOT} {title}"

    lines = [
        f"{E_TICKET} <b>SPORTYTIPS {DOT} {html.escape(title)}</b>",
        (
            f"{E_CAL} {html.escape(req['label'].capitalize())} "
            f"{DOT} times in {bot.LOCAL_TZ_NAME}"
        ),
    ]

    for number, candidate in enumerate(chosen, start=1):

        lines.append("")

        lines.append(
            f"<b>{number}.</b> {E_CLOCK} "
            f"{html.escape(_time_text(candidate, today))}"
            f" {DOT} {E_LEAGUE} {html.escape(candidate['league'])}"
        )

        lines.append(
            f"{E_BALL} {html.escape(candidate['home'])}"
            f" vs {html.escape(candidate['away'])}"
        )

        lines.append(
            f"{E_TIPS} <b>{html.escape(candidate['label'])}</b>"
            f" {DOT} {E_MONEY} {candidate['odd']:.2f}"
            f" {DOT} {_confidence(candidate['p'])} "
            f"{round(candidate['p'] * 100)}%"
        )

        lines.append(f"{E_CHAT} {html.escape(candidate['reason'])}")

    lines += [
        "",
        LINE,
        f"{E_MONEY} <b>Total odds: {total_odds:.2f}</b>",
        (
            f"{E_CHART} Estimated combined chance: <b>"
            + (f"{chance * 100:.1f}%" if chance >= 0.001 else "under 0.1%")
            + "</b>"
        ),
    ]

    if target and not built["reached"]:

        lines.append(
            f"{E_WARN} I could not reach {_fmt(target)} odds. "
            + html.escape(
                _shortfall_text(
                    built, req, target, count, sport_mode, market_only, risk_ok, len(chosen), total_odds
                )
            )
        )

    elif count and not target and len(chosen) < int(count):

        lines.append(
            f"{E_WARN} "
            + html.escape(
                _shortfall_text(
                    built, req, target, count, sport_mode, market_only, risk_ok, len(chosen), total_odds
                )
            )
        )

    if built["note"]:

        lines.append(f"{E_INFO} {html.escape(built['note'])}")

    for candidate, why in swapped:

        lines.append(
            f"{E_SWAP} Swapped {html.escape(candidate['home'])} vs "
            f"{html.escape(candidate['away'])}: {html.escape(why)}"
        )

    mix = _kind_counts(chosen)

    lines.append(
        f"{E_MIX} Mix: "
        + ", ".join(
            f"{number} {KIND_NAME.get(kind, kind).lower()}"
            for kind, number in sorted(mix.items(), key=lambda x: -x[1])
        )
    )

    if code:

        lines.append(
            f"{E_CODE} SportyBet code: <b>{html.escape(str(code))}</b>"
        )

    else:

        lines.append(f"{E_WARN} SportyBet booking code could not be created.")

    if reviewed:

        lines.append(
            f"{E_SCI} Picks were additionally checked for current team news, "
            "form and squad information. Check confirmed line-ups before kickoff."
        )

    if len(chosen) >= 12 and target and target >= 50:

        lines.append(
            f"{E_WARN} Big-odds tickets are naturally high risk. "
            "Stake responsibly."
        )

    if daily:

        lines.append(
            f"{E_LOCK} Daily 2 odds is the same ticket for everyone and "
            "changes once a day. No pick is ever 100% certain."
        )

    lines.append(f"{E_WARN} Predictions are estimates, not guarantees (18+).")

    text_out = "\n".join(lines)

    bot.send_message(chat_id, text_out)

    if daily:
        _daily_save(text_out, req.get("daily_day"))

    # ========================================================
    # TICKET IMAGE
    # ========================================================

    try:

        import ticket_image_lite

        rows = [
            {
                "time": _time_text(candidate, today),
                "league": candidate["league"],
                "match": f"{candidate['home']} vs {candidate['away']}",
                "pick": candidate["label"],
                "odd": candidate["odd"],
                "prob": candidate["p"],
                # extra details so the History page can mark each pick later
                "kind": candidate.get("kind"),
                "home": candidate["home"],
                "away": candidate["away"],
                "kickoff_ts": candidate["kickoff"].timestamp(),
            }
            for candidate in chosen
        ]

        if daily:
            image_title = "Daily 2 Odds"

        elif straight:
            image_title = "Straight Win"

        elif target:
            image_title = f"{_fmt(target)} Odds"

        else:
            image_title = f"{len(chosen)} Picks"

        if market_only is not None:
            image_title = f"Over {market_only:g} {image_title}"

        if sport_mode == "basketball":
            image_title = f"{(league_hint or 'Basketball').upper()} {image_title}"

        elif sport_mode == "mixed":
            image_title = f"Mixed {image_title}"

        png = ticket_image_lite.make_ticket_image(
            rows,
            image_title,
            req["label"].capitalize(),
            total_odds,
            chance,
            code,
        )

        _launcher_send_photo(chat_id, png, "")

        if daily:
            _daily_save_ticket(rows, image_title, req["label"].capitalize(), total_odds, chance, code)

            if not _daily_cache.get("ticket"):
                _daily_save_image(png)

    except Exception as exc:

        print(f"Ticket picture failed: {exc}")


# ============================================================
# DAILY CACHE
# ============================================================

_DAILY_LOCK = threading.Lock()

_DAILY_FILE = os.getenv(
    "DAILY_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "daily_2odds.json",
    ),
)

_daily_cache = {}

# app.py sets this to a small database store, so the Daily ticket survives restarts and
# redeploys and is the same for every user. Without it, a file next to the code is used.
DAILY_STORE = None


def _daily_clock():
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(bot.LOCAL_TZ)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now_utc, local_now, midnight


def _daily_target_day():
    """The day whose Daily 2 odds is on offer right now (same rule _flow_main uses)."""

    now_utc, _, midnight = _daily_clock()
    earliest = now_utc + timedelta(hours=getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1))
    day = midnight

    if midnight + timedelta(hours=DAILY_TO_HOUR) <= earliest:
        day = midnight + timedelta(days=1)

    return day.strftime("%Y-%m-%d")


def _daily_next_flip():
    """Seconds until the Daily 2 odds moves on to the next day's ticket."""

    _, local_now, midnight = _daily_clock()
    flip = midnight + timedelta(
        hours=DAILY_TO_HOUR - getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1)
    )

    if flip <= local_now:
        flip += timedelta(days=1)

    return (flip - local_now).total_seconds()


def _daily_write():

    store = DAILY_STORE

    if store is not None:

        try:

            store.put(dict(_daily_cache))

        except Exception as exc:

            print(f"Daily store save failed: {exc}")

    try:

        with open(_DAILY_FILE, "w", encoding="utf-8") as fh:
            json.dump(_daily_cache, fh)

    except Exception as exc:

        print(f"Daily cache save failed: {exc}")


def _daily_get():

    data = None

    store = DAILY_STORE

    if store is not None:

        try:

            data = store.get()

        except Exception as exc:

            print(f"Daily store read failed: {exc}")

    if not data:

        try:

            with open(_DAILY_FILE, encoding="utf-8") as fh:
                data = json.load(fh)

        except Exception:

            data = dict(_daily_cache) if _daily_cache else None

    if not data:
        return None

    created = data.get("created")

    if not created or not data.get("text"):
        return None

    # A ticket built by older logic (for example all 1UP) is never shown again.
    if data.get("v") != DAILY_VERSION:
        return None

    # One ticket per day for everyone: a ticket built for another day is never shown.
    if data.get("day") != _daily_target_day():
        return None

    # A saved ticket built for another time window (old code) is thrown away.
    if DAILY_WINDOW_TEXT not in str(data.get("text")):
        return None

    # An old saved ticket under 2.00 odds (built by the old code) is thrown away.
    try:
        match = re.search(r"Total odds:\s*(?:<[^>]+>)?\s*([0-9]+\.[0-9]+)", str(data.get("text")))

        if match and float(match.group(1)) < 2.00:
            return None
    except Exception:
        pass

    _daily_cache.clear()
    _daily_cache.update(data)

    return dict(data)


def _daily_save(text_out, day=None):

    _daily_cache.clear()

    _daily_cache.update(
        {
            "created": time.time(),
            "day": day or _daily_target_day(),
            "v": DAILY_VERSION,
            "text": text_out,
        }
    )

    _daily_write()


def _daily_save_image(png):

    import base64

    if _daily_cache.get("text"):

        _daily_cache["image"] = base64.b64encode(png).decode("ascii")

        _daily_write()


def _daily_save_ticket(rows, title, subtitle, total_odds, chance, code):
    """Keep the ticket's data so each person who opens today's Daily 2 odds gets it
    saved in their own History (a replayed picture alone would not be saved)."""

    if not _daily_cache.get("text"):
        return

    try:
        _daily_cache["ticket"] = json.loads(
            json.dumps(
                {
                    "rows": rows,
                    "title": title,
                    "subtitle": subtitle,
                    "total_odds": total_odds,
                    "chance": chance,
                    "code": code,
                },
                default=str,
            )
        )
        _daily_write()

    except Exception as exc:

        print(f"Daily ticket data save failed: {exc}")


def _daily_left(cached):

    left = max(0, _daily_next_flip())

    return int(left // 3600), int((left % 3600) // 60)


def _daily_used(cached, person):

    users = cached.get("users") or {}

    return users.get(str(person)) == cached.get("created")


def _daily_mark(person, created):

    data = _daily_get()

    if not data or data.get("created") != created:
        return

    users = _daily_cache.setdefault("users", {})

    users[str(person)] = created

    _daily_write()


def _daily_replay(chat_id, cached):

    import base64

    hours, minutes = _daily_left(cached)

    bot.send_message(
        chat_id,
        cached["text"]
        + f"\n\n{E_LOCK} You can open Daily 2 odds once a day. "
        f"The next one unlocks in {hours}h {minutes}m.",
    )

    ticket = cached.get("ticket")

    if ticket and ticket.get("rows"):

        # Drawn again from the saved data: same picture, and it lands in this person's History.
        try:

            import ticket_image_lite

            png = ticket_image_lite.make_ticket_image(
                ticket["rows"],
                ticket["title"],
                ticket["subtitle"],
                ticket["total_odds"],
                ticket["chance"],
                ticket.get("code"),
            )

            _launcher_send_photo(chat_id, png, "")
            return

        except Exception as exc:

            print(f"Daily picture redraw failed: {exc}")

    if cached.get("image"):

        try:

            _launcher_send_photo(
                chat_id,
                base64.b64decode(cached["image"]),
                "",
            )

        except Exception as exc:

            print(f"Daily image replay failed: {exc}")


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

    if DAILY_RE.search(text or ""):

        with _DAILY_LOCK:

            cached = _daily_get()

            if cached:

                if _daily_used(cached, chat_id):

                    hours, minutes = _daily_left(cached)

                    bot.send_message(
                        chat_id,
                        f"{E_LOCK} You already opened today's Daily 2 odds. "
                        f"The next one unlocks in {hours}h {minutes}m.",
                    )

                    return

                _daily_replay(chat_id, cached)

                _daily_mark(chat_id, cached["created"])

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
                _daily_mark(chat_id, fresh["created"])

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

_orig_flow = bot.prediction_ticket_flow

bot.prediction_ticket_flow = flow

bot.MAX_DAYS_AHEAD = max(getattr(bot, "MAX_DAYS_AHEAD", 2), 3)


# ---- main.py needs no change: the pieces below are attached to it here ----

# 1) "16,000 odds" is read as 16000 everywhere main.py reads a request.

_orig_parse_request = bot.parse_request


def _parse_request_commas(text):
    # "over 2.5 games" would be read as "5 games": the goals line is taken out for main.py
    return _orig_parse_request(re.sub(r"\bover\s*[0-4]\.5\b", " ", _comma_free(text), flags=re.I))


bot.parse_request = _parse_request_commas


# 2) The AI chat is told about the goals market, big odds and "risky".

_orig_ai_system_prompt = bot.ai_system_prompt

AI_EXTRA = """

EXTRA REQUEST PIECES (these override anything above that says otherwise)
- Goals market (optional): "over 1.5 only", "over 2.5 only" (0.5, 1.5, 2.5, 3.5 and 4.5 work).
  Use it when the user wants a ticket made only of that market, for example "over 1.5 acca"
  or "over 2.5 only today". Copy the number the user says.
- Odds can be any size the user asks for, for example "16000 odds" or "5000 odds".
  Never lower, round or cap the number yourself, and write it as plain digits (no comma).
- If the user says they do not mind risky legs, or wants a big or wild ticket, add "risky".
- "weekend" is a valid window: keep it when the user says this weekend.
- Do not refuse big odds or long tickets. In "reply" you may say that the bigger the odds,
  the lower the chance.
- Basketball: the bot CAN build basketball tickets (match winner, over total points and
  positive handicaps). If the user asks for basketball, NCAA or NBA, keep that word in
  "request", for example "ncaa 100 odds", "basketball 20 odds" or "nba 10 odds today".
  If they want football together with basketball, add the word "mixed", for example
  "safe 10 odds mixed basketball today". Never say basketball is not possible.
"""


def _ai_system_prompt_extra():
    return _orig_ai_system_prompt() + AI_EXTRA


bot.ai_system_prompt = _ai_system_prompt_extra


# 3) If the AI still drops the market, the risk word, the odds or the weekend the person
#    typed, they are put back before the ticket is built.

_orig_ai_dispatch = bot.ai_dispatch

_ODDS_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:total\s*)?odds?\b", re.I)


def _keep_user_extras(user_text, request):

    low = _comma_free(user_text).lower()
    out = _comma_free(request).strip() or low

    # the goals market (over 2.5 only, over 1.5 acca ...)
    match = OVER_ONLY_RE.search(low)

    if match and not OVER_ONLY_RE.search(out):
        out += f" over {match.group(1)} only"

    # "I don't mind risky legs"
    if RISK_OK_RE.search(low) and not RISK_OK_RE.search(out):
        out += " risky"

    # the odds the person typed: the AI must not change them
    asked = _ODDS_IN_TEXT.search(low)

    if asked:

        try:
            wanted = float(asked.group(1))
        except Exception:
            wanted = 0.0

        if wanted >= 1.5:

            given = _ODDS_IN_TEXT.search(out)
            same = False

            if given:
                try:
                    same = abs(float(given.group(1)) - wanted) < 1e-9
                except Exception:
                    same = False

            if not same:
                out = _ODDS_IN_TEXT.sub(" ", out)
                out = f"{out} {asked.group(1)} odds"

    # this weekend
    if re.search(r"\bweekend\b", low) and not re.search(r"\bweekend\b", out):

        out = re.sub(
            r"\b(today|tonight|tomorrow|week|\d+\s*days?)\b",
            " ",
            out,
        ) + " weekend"

    # basketball / NCAA / NBA, and "mixed with football"
    found = BB_WORD_RE.search(low)

    if found and not BB_WORD_RE.search(out):
        out += " " + found.group(0)

    if found and BB_MIXED_RE.search(low) and not BB_MIXED_RE.search(out):
        out += " mixed"

    return re.sub(r"\s+", " ", out).strip()


def _ai_dispatch_extras(chat_id, text, data):

    try:

        if str(data.get("action", "chat")).lower() == "ticket":

            data = dict(data)
            data["request"] = _keep_user_extras(text, str(data.get("request", "")))

    except Exception as exc:
        print(f"Keeping the request details failed: {exc}")

    return _orig_ai_dispatch(chat_id, text, data)


bot.ai_dispatch = _ai_dispatch_extras


# ============================================================
# EDIT TICKET: MAKE A PASTED CODE A "SURE" TICKET
# ============================================================
#
# Uses the SAME steps as the prediction: the markets of the game, the football
# stats, the odds floor, the 1UP / 2UP and GG checks, and the chance of each pick.
#
#   pick passes every check and its chance is at least SURE_MIN_P  -> kept
#   pick fails                                                      -> swapped for the
#                                                                      best-chance pick
#                                                                      in the SAME game
#   the game has no pick that passes                                -> removed

SURE_MIN_P = _env_float("SURE_MIN_P", 0.62)


class SaferError(Exception):
    """A plain-words problem for the person (app.py shows it as it is)."""


class _CodeGames:
    """Lets gather() look at only the games on one booking code."""

    def __init__(self, provider, events):
        self._provider = provider
        self._events = events

    def get_upcoming(self, start, end):
        return list(self._events)

    def __getattr__(self, name):
        return getattr(self._provider, name)


def _label_side(label, home, away):

    text = str(label or "").lower()
    h, a = str(home or "").lower(), str(away or "").lower()

    if h and h in text and not (a and a in text):
        return "home"

    if a and a in text and not (h and h in text):
        return "away"

    return None


def _edit_fail_reason(leg, group, original):
    """Plain words for why the original pick did not pass."""

    if original is not None:
        return f"Only about a {round(original.get('p', 0) * 100)}% chance"

    odd = leg.get("odd") or 0.0

    if odd and odd < NORMAL_MIN_ODD:
        return f"Odds of {odd:.2f} are too low"

    label = sp.leg_label(leg)
    low = label.lower()
    facts = next((c.get("facts") for c in group if c.get("facts")), None)

    try:

        up = re.search(r"\((\d)\s*up\)", low)

        if up and facts:

            side = _label_side(label, leg.get("home"), leg.get("away"))

            if side:

                probe = {
                    "kind": "up",
                    "side": side,
                    "up_level": int(up.group(1)),
                    "facts": facts,
                }

                if not _up_supported(probe):

                    venue = (facts.get("venue") or {}).get(side) or {}
                    played = venue.get("played") or 0
                    scored_in = venue.get("scored_in")
                    where = "home" if side == "home" else "away"

                    if played and scored_in is not None:
                        return f"Scored in only {scored_in} of {played} {where} games"

                    return "This team does not score or the other side does not concede enough"

        if "both teams to score" in low and "not" not in low and facts:

            if not _btts_supported(facts):
                return "Both teams do not score often enough"

    except Exception:
        pass

    return "Did not pass the prediction checks"


def safer_rebuild(provider, code):

    legs = provider.load_code(code)

    events = {}

    for leg in legs:

        raw = dict(leg.get("event_raw") or {})
        raw.setdefault("eventId", leg["event_id"])
        raw.setdefault("homeTeamName", leg["home"])
        raw.setdefault("awayTeamName", leg["away"])

        events.setdefault(leg["event_id"], raw)

    now = datetime.now(timezone.utc)

    groups, _, _, _ = gather(
        _CodeGames(provider, list(events.values())),
        now,
        now + timedelta(days=30),
        "normal",
        NORMAL_MIN_ODD,
        frozenset(),
    )

    by_event = {}

    for group in groups:
        if group:
            by_event[group[0]["event_id"]] = group

    new_legs = []
    dropped = []
    kept = swapped = 0
    seen = set()

    for leg in legs:

        group = by_event.get(leg["event_id"]) or []
        match = f"{leg['home']} vs {leg['away']}"

        original = next((c for c in group if c["key"] == leg["key"]), None)
        good = [c for c in group if c.get("p", 0) >= SURE_MIN_P]

        if original is not None and original in good:
            pick = original
            reason = "Kept: " + _reason_for(original)
            was_kept = True

        else:

            was_kept = False

            if not good:

                dropped.append(
                    {
                        "match": match,
                        "reason": (
                            "No pick in this game passes the checks"
                            if group
                            else "No open market (the game may have started)"
                        ),
                    }
                )

                continue

            pick = max(good, key=lambda c: (c.get("p", 0), c["odd"]))

            reason = (
                _edit_fail_reason(leg, group, original)
                + ". Swapped for the safest pick in this game: "
                + _reason_for(pick)
            )

        identity = (leg["event_id"], pick["key"])

        if identity in seen:
            continue

        seen.add(identity)

        if was_kept:
            kept += 1
        else:
            swapped += 1

        new_legs.append(
            {
                "event_id": leg["event_id"],
                "home": leg["home"],
                "away": leg["away"],
                "key": pick["key"],
                "odd": pick["odd"],
                "label_override": pick["label"],
                "reason": reason,
            }
        )

    if not new_legs:
        raise SaferError("No game on this ticket has a safe pick right now.")

    changed = bool(swapped or dropped)

    new_code = code

    if changed:
        new_code = provider._save_code([(l["event_id"], l["key"]) for l in new_legs])

    result = sp.summarize_legs(new_legs)

    result["code"] = new_code
    result["changed"] = changed
    result["dropped"] = dropped
    result["counts"] = {"kept": kept, "swapped": swapped, "dropped": len(dropped)}

    return result
