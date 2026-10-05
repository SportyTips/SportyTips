"""SportyTips Smart SportyBet ticket builder.

Uses SportyBet's own fixtures, markets and odds.

Important:
- No API-Football.
- No fake booking codes.
- Booking codes come from SportyBet provider.
- No DNB.
- No negative handicaps.
- No Under selections.
- Supports 1UP / 2UP.
- Supports Over goals.
- Supports BTTS.
- Supports team goals.
- Supports corners.
- Supports positive handicap.
- Supports Asian handicap.
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
from datetime import (
    datetime,
    timedelta,
    timezone,
)

import main as bot
import sportybet_provider as sp


BRAND = "SPORTYTIPS"


# ============================================================
# SPEED SETTINGS
# ============================================================

MAX_DETAIL_EVENTS = 10

DETAIL_WORKERS = 6

DETAIL_SECONDS = 7

MAX_CACHED_MATCHES = 150

MAX_PAGES = 4

MAX_LEGS = 30

MAX_OPTIONS_PER_MATCH = 5

GROUP_LIMIT = 80

OVERSHOOT = 0.06


# ============================================================
# MARKET SETTINGS
# ============================================================

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


MAX_ODDS_KIND = {
    "corners": 1.50,
    "corners_1h": 1.50,
}


KIND_BONUS = {
    "up": 0.04,
    "team_goals": 0.04,
    "corners": 0.03,
    "corners_1h": 0.03,
    "handicap": 0.03,
    "asian_handicap": 0.03,
    "btts": 0.02,
    "streak": 0.03,
    "dc": 0.01,
}


KIND_CAP = {
    "over15": 0.20,
    "over": 0.30,
    "btts": 0.30,
    "dc": 0.30,
    "up": 0.40,
    "corners": 0.30,
    "corners_1h": 0.20,
    "handicap": 0.30,
    "asian_handicap": 0.25,
    "team_goals": 0.35,
    "streak": 0.25,
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
# HELPERS
# ============================================================

def _f(value):
    try:
        return sp._float(value)
    except Exception:
        return None


def _single(odd):

    if not odd:
        return None

    return min(
        0.95 / odd,
        0.97,
    )


def _two_way(
    odd,
    other,
):

    if not odd:
        return None

    if other:

        a = 1 / odd
        b = 1 / other

        total = a + b

        if total:
            return a / total

    return min(
        0.95 / odd,
        0.97,
    )


# ============================================================
# SIDE DETECTION
# ============================================================

def _side_of(
    outcome,
    two_way=False,
):

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

        up_number = int(
            found.group(1)
        )

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
                _single(odd),
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
            or not probability
            or odd <= 1
        ):
            return

        item = {
            "kind": kind,
            "label": label,
            "odd": odd,
            "p": probability,
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

    ph = None
    pa = None

    if h and d and a:

        inv = [
            1 / h,
            1 / d,
            1 / a,
        ]

        total = sum(inv)

        if total:
            ph = inv[0] / total
            pa = inv[2] / total

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
    # DOUBLE CHANCE
    # ========================================================

    if ph is not None and pa is not None:

        key = (
            sp.M_DC,
            "",
            sp.OUT_DC["12"],
        )

        odd = sp.find_odds(
            markets,
            key,
        )

        add(
            "dc",
            f"{home} or {away}",
            odd,
            ph + pa,
            key,
        )

    # ========================================================
    # OVER GOALS ONLY
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
    # BTTS
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

            # NEVER select negative handicap.
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
            )

    return candidates


# ============================================================
# FAVOURITE STRENGTH
# ============================================================

def favourite_strength(
    event
):

    markets = (
        event.get(
            "markets"
        )
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
        return None

    inv = [
        1 / h,
        1 / d,
        1 / a,
    ]

    total = sum(inv)

    if not total:
        return None

    return max(
        inv[0],
        inv[2],
    ) / total


# ============================================================
# DETAILED MARKET CACHE
# ============================================================

_DETAIL_LOCK = threading.Lock()


def _slim_markets(
    markets
):

    keep = []

    for market in markets or []:

        market_id = str(
            market.get("id")
        )

        if market_id in {
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
        }:
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

        self._sportytips_market_cache = (
            cache
        )

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


# Replace provider's detailed cache.
sp.SportyBetProvider._event_markets_cached = (
    _event_markets_cached
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

    started = time.time()

    # This uses the provider's own SportyBet
    # event loader.
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

    # ========================================================
    # RANK EVENTS
    # ========================================================

    rated = []

    for event in events:

        strength = favourite_strength(
            event
        )

        if strength is not None:

            rated.append(
                (
                    strength,
                    event,
                )
            )

    rated.sort(
        key=lambda item:
            item[0],
        reverse=True,
    )

    # If the provider's event list does
    # not contain markets, don't throw
    # everything away.
    #
    # Use the first events instead.
    if rated:

        wanted = [
            event
            for _, event
            in rated[
                :MAX_DETAIL_EVENTS
            ]
        ]

    else:

        wanted = [
            event
            for event in events[
                :MAX_DETAIL_EVENTS
            ]
            if event.get(
                "eventId"
            )
        ]

    # ========================================================
    # LOAD DETAILS IN PARALLEL
    # ========================================================

    details = {}

    if wanted:

        pool = ThreadPoolExecutor(
            max_workers=DETAIL_WORKERS
        )

        futures = {
            pool.submit(
                provider._event_markets_cached,
                event[
                    "eventId"
                ],
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
                "Detailed market loading "
                "timed out."
            )

        finally:

            pool.shutdown(
                wait=False,
                cancel_futures=True,
            )

    # ========================================================
    # PROBABILITY SETTINGS
    # ========================================================

    min_p = (
        {
            "safe": 0.65,
            "normal": 0.55,
            "risky": 0.45,
        }.get(
            risk,
            0.55,
        )
        - min_p_shift
    )

    max_odd = (
        {
            "safe": 2.20,
            "normal": 2.40,
            "risky": 2.60,
        }.get(
            risk,
            2.40,
        )
    )

    if straight_only:

        min_p = min(
            min_p,
            0.40,
        )

        max_odd = max(
            max_odd,
            2.60,
        )

    groups = []

    # ========================================================
    # CREATE CANDIDATES
    # ========================================================

    for event in events:

        event_id = event.get(
            "eventId"
        )

        if not event_id:
            continue

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

        fixture = sp.sporty_fixture(
            event
        )

        candidates = event_candidates(
            event,
            markets,
        )

        kept = []

        labels = set()

        for candidate in candidates:

            if (
                straight_only
                and candidate[
                    "kind"
                ]
                != "up"
            ):
                continue

            floor_kind = MIN_ODDS.get(
                candidate[
                    "kind"
                ],
                1.30,
            )

            cap_kind = (
                MAX_ODDS_KIND.get(
                    candidate[
                        "kind"
                    ]
                )
            )

            odd = candidate[
                "odd"
            ]

            if odd < floor_kind:
                continue

            if (
                cap_kind
                and odd > cap_kind
            ):
                continue

            if candidate[
                "p"
            ] < min_p:
                continue

            if odd > max_odd:
                continue

            if (
                event_id,
                candidate[
                    "key"
                ],
            ) in exclude:
                continue

            if candidate[
                "label"
            ] in labels:
                continue

            labels.add(
                candidate[
                    "label"
                ]
            )

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
                    "league": (
                        fixture.get(
                            "league",
                            {},
                        ).get(
                            "name",
                            "Football",
                        )
                    ),
                }
            )

            kept.append(
                candidate
            )

        if kept:

            kept.sort(
                key=lambda candidate:
                    candidate[
                        "p"
                    ]
                    + KIND_BONUS.get(
                        candidate[
                            "kind"
                        ],
                        0,
                    ),
                reverse=True,
            )

            groups.append(
                kept[
                    :MAX_OPTIONS_PER_MATCH
                ]
            )

    groups.sort(
        key=lambda group:
            max(
                candidate[
                    "p"
                ]
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
        "gather:",
        len(events),
        "matches,",
        len(details),
        "detailed,",
        len(groups),
        "usable,",
        f"{elapsed:.1f}s",
    )

    return (
        groups,
        len(events),
        len(details),
        0,
    )


# ============================================================
# ODDS PRODUCT
# ============================================================

def _product(
    picks
):

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

    INF = float(
        "inf"
    )

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

            odd = candidate[
                "odd"
            ]

            probability = candidate[
                "p"
            ]

            if (
                odd <= 1
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

            cost = (
                -math.log(
                    probability
                )
                - KIND_BONUS.get(
                    candidate[
                        "kind"
                    ],
                    0,
                )
                + 0.06
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

                if (
                    value
                    < new[
                        x
                        + weight
                    ]
                ):

                    new[
                        x
                        + weight
                    ] = value

                    choice[
                        x
                        + weight
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

    return picked


def _dp_exact(
    groups,
    target,
):

    best = None

    aim = target

    for _ in range(3):

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

def _kind_counts(
    picks
):

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

    # Fallback.
    ranked = []

    for group in groups:

        best = max(
            group,
            key=lambda candidate:
                candidate[
                    "p"
                ]
                + KIND_BONUS.get(
                    candidate[
                        "kind"
                    ],
                    0,
                ),
        )

        ranked.append(
            best
        )

    ranked.sort(
        key=lambda candidate:
            candidate[
                "p"
            ],
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
            candidate[
                "p"
            ]
            + KIND_BONUS.get(
                candidate[
                    "kind"
                ],
                0,
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
# REASONS
# ============================================================

def _reason_for(
    candidate
):

    kind = candidate[
        "kind"
    ]

    odd = candidate[
        "odd"
    ]

    home = candidate[
        "home"
    ]

    away = candidate[
        "away"
    ]

    side = candidate.get(
        "side"
    )

    if kind == "up":

        team = (
            home
            if side == "home"
            else away
        )

        number = candidate.get(
            "up_n",
            2,
        )

        return (
            f"{team} to win "
            f"({number}UP) at "
            f"{odd:.2f}."
        )

    if kind == "dc":

        return (
            f"{home} or {away} "
            f"at {odd:.2f}."
        )

    if kind in (
        "over15",
        "over",
    ):

        line = candidate.get(
            "line",
            1.5,
        )

        return (
            f"Over {line:g} goals "
            f"at {odd:.2f}."
        )

    if kind == "btts":

        return (
            "Both teams to score "
            f"at {odd:.2f}."
        )

    if kind == "team_goals":

        line = candidate.get(
            "line",
            0.5,
        )

        team = (
            home
            if side == "home"
            else away
        )

        return (
            f"{team} to score "
            f"{line:g}+ at "
            f"{odd:.2f}."
        )

    if kind == "streak":

        return (
            "No team to score "
            f"3+ in a row at "
            f"{odd:.2f}."
        )

    if kind == "corners":

        line = candidate.get(
            "line",
            7.5,
        )

        return (
            f"Over {line:g} corners "
            f"at {odd:.2f}."
        )

    if kind == "corners_1h":

        line = candidate.get(
            "line",
            3.5,
        )

        return (
            f"1st half Over "
            f"{line:g} corners "
            f"at {odd:.2f}."
        )

    return (
        f"{candidate['label']} "
        f"at {odd:.2f}."
    )


# ============================================================
# DISPLAY
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


def _confidence(
    probability
):

    if probability >= 0.80:
        return "🟢"

    if probability >= 0.70:
        return "🟡"

    return "🟠"


def _fmt(
    value
):

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
            1.35,
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
            0.05
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

    straight_only = bool(
        re.search(
            r"straight\s*-?\s*win",
            text,
            re.I,
        )
    )

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

    target = req.get(
        "target_odds"
    )

    count = req.get(
        "picks"
    )

    risk = (
        req.get("risk")
        or "normal"
    )

    if not target and not count:
        count = 5

    # ========================================================
    # INTRO
    # ========================================================

    if straight_only:

        intro = (
            "⏳ Checking SportyBet "
            "1UP / 2UP matches..."
        )

    elif target:

        intro = (
            "⏳ Checking SportyBet "
            f"matches for "
            f"{_fmt(target)} odds..."
        )

    else:

        intro = (
            "⏳ Checking SportyBet "
            f"matches for "
            f"{count} picks..."
        )

    bot.send_message(
        chat_id,
        intro,
    )

    started = time.time()

    # ========================================================
    # STRAIGHT WIN MODE
    # ========================================================

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
                if straight_today
                else None
            ),
        )

    except Exception as exc:

        traceback.print_exc()

        _reset_straight()

        bot.send_message(
            chat_id,
            (
                "❌ SportyBet could not "
                "be read right now.\n"
                + html.escape(
                    str(exc)[:200]
                )
            ),
        )

        return

    elapsed = (
        time.time()
        - started
    )

    chosen = built[
        "chosen"
    ]

    print(
        f"Ticket built in "
        f"{elapsed:.1f}s: "
        f"{len(chosen)} picks, "
        f"target={target}, "
        f"straight={straight_only}"
    )

    # ========================================================
    # NO PICKS
    # ========================================================

    if not chosen:

        bot.send_message(
            chat_id,
            (
                "❌ I couldn't find "
                "enough usable SportyBet "
                "selections in that window."
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
                f"❌ Today only reaches "
                f"about {actual:.1f} odds, "
                f"not {_fmt(target)}.\n\n"
                "Try a long straight-win "
                "ticket to search more days."
            ),
        )

        _reset_straight()

        return

    # ========================================================
    # SORT
    # ========================================================

    chosen.sort(
        key=lambda candidate:
            candidate[
                "kickoff"
            ]
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
    ]

    for number, candidate in enumerate(
        chosen,
        start=1,
    ):

        lines.append("")

        lines.append(
            f"<b>{number}.</b> "
            f"🕒 "
            f"{html.escape("
                _time_text(
                    candidate,
                    today,
                )
            )}"
            f" • 🏆 "
            f"{html.escape(
                candidate['league']
            )}"
        )

        lines.append(
            f"⚽ "
            f"{html.escape(
                candidate['home']
            )}"
            f" vs "
            f"{html.escape(
                candidate['away']
            )}"
        )

        lines.append(
            f"✅ <b>"
            f"{html.escape(
                candidate['label']
            )}"
            f"</b> • 💰 "
            f"{candidate['odd']:.2f}"
            f" • "
            f"{_confidence(
                candidate['p']
            )} "
            f"{round(
                candidate['p']
                * 100
            )}%"
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
                f"probability: "
                f"<b>"
                f"{chance * 100:.1f}%"
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
            f"⚠️ Could not safely reach "
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
# BACKGROUND WARMER
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

        # First refresh.
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