"""Smart ticket builder for SportyTips.

SportyBet ticket flow.

Main rules:
  * SportyBet supplies the available fixtures, markets and booking-code data.
  * Football evidence from football_data.py is used to support the prediction.
  * Bookmaker odds are used for market availability and ticket construction,
    NOT as the football evidence itself.
  * No Draw No Bet.
  * No Under markets.
  * No yellow-card markets.
  * No negative handicaps.
  * Supports 1UP / 2UP, Over markets, BTTS, positive handicaps,
    corners, either-half markets and suitable team-goal markets.
  * Searches across the available SportyBet fixtures instead of only
    examining the strongest bookmaker favourites.
"""

import html
import importlib
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json

import main as bot
import sportybet_provider as sp

# main.py may not define these, so read them safely.
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
# SETTINGS
# ============================================================

MIN_LEG_PROB = {
    "safe": 0.74,
    "normal": 0.68,
    "risky": 0.55,
}

MAX_LEG_ODDS = {
    "safe": 1.90,
    "normal": 2.20,
    "risky": 3.20,
}

MAX_LEGS = 30

# Maximum number of matches whose complete markets we try to retrieve.
MAX_DETAIL_EVENTS = int(os.getenv("MAX_DETAIL_EVENTS", "70"))

DETAIL_WORKERS = int(os.getenv("DETAIL_WORKERS", "12"))
DETAIL_SECONDS = int(os.getenv("DETAIL_SECONDS", "40"))

OVERSHOOT = 0.06

GROUP_LIMIT = int(os.getenv("GROUP_LIMIT", "120"))

# Double chance disabled.
ALLOW_DOUBLE_CHANCE = False

# Football-data picks receive a bonus.
DATA_BONUS = 0.12

USE_AI_REVIEW = True
WEB_SEARCHES = 2
MAX_DROPS = 4

# Markets we actually want.
KIND_BONUS = {
    "up": 0.05,
    "corners": 0.04,
    "handicap": 0.04,
    "either_half": 0.03,
    "btts": 0.02,
    "over15": 0.02,
    "over": 0.02,
    "team_goals": 0.03,
}

LEG_PENALTY = 0.06

# No Under. No DNB. No double chance.
KIND_CAP = {
    "over15": 0.20,
    "over": 0.30,
    "btts": 0.25,
    "up": 0.40,
    "either_half": 0.25,
    "corners": 0.30,
    "handicap": 0.30,
    "team_goals": 0.25,
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
    """Bookmaker-implied two-way probability.

    This is only used as an initial market baseline.
    football_data.py must adjust the final probability.
    """

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
    """Return home / away."""

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
    """Only allow a genuinely positive handicap."""

    try:
        return float(line) > 0
    except Exception:
        return False


def _candidate_is_allowed(kind, label=""):
    """Final market safety filter."""

    kind = str(kind or "").lower()
    label = str(label or "").lower()

    # Explicitly forbidden.
    if kind in {"under", "dnb", "dc", "yellow_cards", "cards"}:
        return False

    if "draw no bet" in label:
        return False

    if "under " in label:
        return False

    if "yellow card" in label or "cards" in label:
        return False

    # Never allow negative handicap.
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
# CANDIDATES FROM ONE MATCH
# ============================================================

def event_candidates(event, markets):
    """Build usable SportyBet selections from one fixture."""

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
    pa = None

    # ========================================================
    # 1X2
    # ========================================================

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

        # Double chance deliberately disabled.
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

    # ========================================================
    # TOTAL GOALS
    # ========================================================

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

        # OVER ONLY.
        add(
            ("over15" if line == 1.5 else "over"),
            f"Over {line:g} goals",
            over,
            _two_way(over, under),
            (mid, spec, sp.OUT_TOTAL["over"]),
            line=line,
        )

        # Under deliberately not added.

    # ========================================================
    # BTTS
    # ========================================================

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

    # ========================================================
    # OTHER MARKETS
    # ========================================================

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

        # ====================================================
        # 1UP / 2UP
        # ====================================================

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

        # ====================================================
        # WIN EITHER HALF
        # ====================================================

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

                    if (
                        side in label
                        or (side in name and label == "yes")
                    ):

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

        # ====================================================
        # DNB IGNORED
        # ====================================================

        if "draw no bet" in name:
            continue

        # ====================================================
        # CORNERS
        # ====================================================

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
                (mid, spec, over_id),
                line=line,
            )

            continue

        # ====================================================
        # POSITIVE HANDICAP ONLY
        # ====================================================

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

            try:
                line = float(spec.replace("hcp=", ""))
            except ValueError:
                continue

            if not _is_positive_handicap(line):
                continue

            odds = [_f(o.get("odds")) for o in outcomes]

            for outcome, odd, other in zip(outcomes, odds, odds[::-1]):

                side = _side_of(outcome, two_way=True)

                if not side or not odd:
                    continue

                team = home if side == "home" else away

                label = f"{team} +{line:g} handicap"

                add(
                    "handicap",
                    label,
                    odd,
                    _two_way(odd, other),
                    (mid, spec, str(outcome.get("id"))),
                    side=side,
                    handicap=line,
                )

            continue

        # ====================================================
        # TEAM GOALS
        # ====================================================

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
# FOOTBALL DATA STUDY
# ============================================================

def study(groups, notify=None):
    """Study football evidence for available matches."""

    import football_data as fd

    if fd.ENRICH_MAX <= 0 or not groups:
        return 0

    top = groups[:fd.ENRICH_MAX]

    if notify:
        notify(len(top))

    studied = 0
    started = time.time()

    for group in top:

        if time.time() - started > fd.ENRICH_SECONDS:
            break

        try:

            event = group[0]["event"]

            fixture = fd.find_fixture(event)

            if not fixture:
                continue

            facts = fd.get_facts(fixture)

            if not facts:
                continue

            studied += 1

            for candidate in group:

                try:
                    candidate["p"] = fd.adjusted_p(candidate, facts)

                except Exception:
                    candidate["p"] = 0.0
                    continue

                candidate["facts"] = facts
                candidate["has_data"] = True

        except Exception as exc:
            print(f"Football study failed: {exc}")

    return studied


# ============================================================
# SPORTYBET MATCH RANKING
# ============================================================

def favourite_strength(event):
    """Initial market availability ranking."""

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


# ============================================================
# GATHER SPORTYBET FIXTURES
# ============================================================

def _detail_order(events):
    """Spread market-detail retrieval across the fixture list."""

    if len(events) <= MAX_DETAIL_EVENTS:
        return list(events)

    ranked = sorted(events, key=favourite_strength, reverse=True)

    selected = []
    seen = set()

    priority_count = min(MAX_DETAIL_EVENTS // 2, len(ranked))

    for event in ranked[:priority_count]:

        eid = event.get("eventId")

        if eid in seen:
            continue

        selected.append(event)
        seen.add(eid)

    remaining = MAX_DETAIL_EVENTS - len(selected)

    if remaining > 0:

        step = max(1, len(events) // remaining)

        for index in range(0, len(events), step):

            event = events[index]
            eid = event.get("eventId")

            if eid in seen:
                continue

            selected.append(event)
            seen.add(eid)

            if len(selected) >= MAX_DETAIL_EVENTS:
                break

    return selected[:MAX_DETAIL_EVENTS]


def gather(
    provider,
    start,
    end,
    risk,
    floor,
    exclude,
    min_p_shift=0.0,
    notify=None,
):
    """Gather SportyBet markets and football evidence."""

    events = provider.get_upcoming(start, end)

    if not events:
        return [], 0, 0, 0

    wanted = _detail_order(events)

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

                for future in as_completed(futures, timeout=DETAIL_SECONDS):

                    event = futures[future]

                    try:
                        details[event["eventId"]] = future.result()
                    except Exception:
                        pass

            except FutureTimeout:
                pass

    min_p = (
        MIN_LEG_PROB.get(risk, MIN_LEG_PROB["normal"])
        - min_p_shift
    )

    max_odd = MAX_LEG_ODDS.get(risk, MAX_LEG_ODDS["normal"])

    groups = []

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

        kept = []
        seen = set()

        for candidate in event_candidates(event, markets):

            if not _candidate_is_allowed(
                candidate["kind"],
                candidate["label"],
            ):
                continue

            if not (floor <= candidate["odd"] <= max_odd):
                continue

            if candidate["p"] < min_p:
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

            candidate.update(
                {
                    "event": event,
                    "event_id": event["eventId"],
                    "home": event.get("homeTeamName", "Home"),
                    "away": event.get("awayTeamName", "Away"),
                    "kickoff": kickoff,
                    "league": league_data.get("name", "Football"),
                    "has_data": False,
                }
            )

            kept.append(candidate)

        if kept:
            groups.append(kept)

    groups.sort(
        key=lambda group: max(c["p"] for c in group),
        reverse=True,
    )

    groups = groups[:GROUP_LIMIT]

    studied = study(groups, notify)

    # Only football-evidence-backed candidates
    # are allowed into the final ticket.
    evidence_groups = []

    for group in groups:

        valid = []

        for candidate in group:

            if not candidate.get("has_data"):
                continue

            if candidate.get("p", 0) < min_p:
                continue

            valid.append(candidate)

        if valid:
            evidence_groups.append(valid)

    return (
        evidence_groups,
        len(events),
        len(details),
        studied,
    )


# ============================================================
# ODDS COMBINATION
# ============================================================

def _dp(groups, target):
    """Find a combination around the requested odds target."""

    if not groups or not target:
        return None

    S = 200

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

            if (
                not odd
                or odd <= 1
                or not probability
                or probability <= 0
            ):
                continue

            try:

                weight = round(math.log(odd) * S)

                cost = (
                    -math.log(probability)
                    - KIND_BONUS.get(candidate.get("kind"), 0.0)
                    + LEG_PENALTY
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

    return picked[:MAX_LEGS]


def _product(picks):
    total = 1.0

    for candidate in picks:
        total *= candidate["odd"]

    return total


def _dp_exact(groups, target):
    """Search and correct for real odds."""

    if not groups:
        return None

    aim = target
    best = None

    for _ in range(6):

        picks = _dp(groups, aim)

        if not picks:
            break

        actual = _product(picks)

        if (
            actual >= target
            and (best is None or actual < _product(best))
        ):
            best = picks

        if target <= actual <= target * (1 + OVERSHOOT + 0.02):
            return picks

        if actual <= 0:
            break

        aim = max(target, aim * target / actual * 1.004)

    return best


# ============================================================
# MARKET MIXING
# ============================================================

def _prune(groups, legs, scale):
    """Prevent one market from taking over."""

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

        kept = [
            candidate
            for candidate in group
            if id(candidate) not in drop
        ]

        if kept:
            pruned.append(kept)

    return pruned


def _kind_counts(chosen):
    counts = {}

    for candidate in chosen:

        kind = candidate["kind"]

        counts[kind] = counts.get(kind, 0) + 1

    return counts


def choose_target(groups, target):
    """Choose an evidence-backed ticket around target odds."""

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

    median_weight = all_weights[len(all_weights) // 2]

    legs = max(
        3,
        min(
            MAX_LEGS,
            round(math.log(target) / max(median_weight, 0.05)),
        ),
    )

    scale = 1.0

    for _ in range(5):

        for _ in range(4):

            pruned = _prune(groups, legs, scale)

            if not pruned:
                scale *= 1.6
                continue

            chosen = _dp_exact(pruned, target)

            if chosen:
                break

            scale *= 1.6

        else:
            chosen = None

        if not chosen:
            break

        counts = _kind_counts(chosen)

        valid_mix = True

        for kind, share in KIND_CAP.items():

            allowed = max(
                1,
                math.ceil(share * len(chosen) * scale),
            )

            if counts.get(kind, 0) > allowed:
                valid_mix = False
                break

        if valid_mix:
            return chosen, True

        legs = len(chosen)

    return [], False


def choose_count(groups, count):
    """Choose requested evidence-backed picks."""

    if not groups or count <= 0:
        return [], False

    count = min(int(count), MAX_LEGS)

    ranked = sorted(
        (
            candidate
            for group in groups
            for candidate in group
            if candidate.get("has_data")
        ),
        key=lambda candidate: (
            candidate.get("p", 0)
            + KIND_BONUS.get(candidate.get("kind"), 0)
            + (DATA_BONUS if candidate.get("has_data") else 0)
        ),
        reverse=True,
    )

    chosen = []

    for scale in (1.0, 1.6, 2.5, 4.0):

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

            if counts.get(kind, 0) >= cap:
                continue

            chosen.append(candidate)
            used_events.add(event_id)
            counts[kind] = counts.get(kind, 0) + 1

            if len(chosen) >= count:
                return chosen, True

    return chosen, len(chosen) >= count


# ============================================================
# AI REVIEW
# ============================================================

def _ai_json(prompt, system):
    """Ask AI for news/reason review."""

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

            with urlopen(request, timeout=40) as response:

                data = json.loads(response.read().decode("utf-8"))

        except (HTTPError, URLError, ValueError) as exc:

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


REVIEW_SYSTEM = """You are the analyst behind SportyTips.

Each pick has football FACTS from last-5 form, goals and head-to-head data.

Write one short reason for every pick.

Rules:
- Use only facts actually supplied.
- Never invent statistics.
- Never use bookmaker odds as evidence.
- Never mention bookmaker pricing.
- Never recommend Draw No Bet.
- Never recommend Under.
- Never recommend yellow-card markets.
- Do not guarantee a result.
- If news clearly hurts a selection, mark it "drop".
- Otherwise mark it "keep".

Reply ONLY with:
{"picks":[{"i":0,"reason":"...","flag":"keep"}]}
"""


def ai_review(chosen, local_now):
    import football_data as fd

    lines = []

    for i, candidate in enumerate(chosen):

        when = candidate["kickoff"].astimezone(
            bot.LOCAL_TZ
        ).strftime("%a %H:%M")

        facts = candidate.get("facts")

        # Calculate the draft reason separately to avoid a nested f-string.
        draft_reason = fd.reason_for(
            candidate,
            facts,
            candidate["home"],
            candidate["away"],
        )

        lines.append(
            f"{i}. "
            f"{candidate['home']} vs "
            f"{candidate['away']} "
            f"({candidate['league']}) "
            f"kickoff {when} | "
            f"pick: {candidate['label']}\n"
            f"FACTS: "
            f"{fd.facts_digest(facts)}\n"
            f"DRAFT: "
            f"{draft_reason}"
        )

    prompt = (
        f"Today is "
        f"{local_now.strftime('%A %d %B %Y')} "
        f"(Nigeria time).\n\n"
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
        except (TypeError, ValueError):
            continue

        if not (0 <= index < len(chosen)):
            continue

        reason = str(item.get("reason", "")).strip()[:240]

        flag = (
            "drop"
            if (
                str(item.get("flag", "")).lower() == "drop"
                and reason
            )
            else "keep"
        )

        if reason:

            candidate = chosen[index]

            reasons[
                (candidate["event_id"], candidate["key"])
            ] = (reason, flag)

    return reasons


# ============================================================
# BUILD TICKET
# ============================================================

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
        return "🟢"

    if p >= 0.70:
        return "🟡"

    return "🟠"


def build_ticket(
    provider,
    req,
    target,
    count,
    risk,
    exclude=frozenset(),
    notify=None,
):
    """Build only from football-evidence-backed selections."""

    if target:

        floor = max(
            1.10,
            min(1.45, target ** (1 / 26)),
        )

    else:
        floor = 1.15

    if risk == "risky":
        floor = max(floor, 1.35)

    extra_days = 0
    note = ""

    while extra_days <= 2:

        end = req["end"] + timedelta(days=extra_days)

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
            (0.05 if (target and target <= 20) else 0.0),
            notify,
        )

        if not groups:
            extra_days += 1
            continue

        if target:
            chosen, reached = choose_target(groups, target)
        else:
            chosen, reached = choose_count(groups, count)

        if chosen:
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


# ============================================================
# MAIN FLOW
# ============================================================

def flow(
    chat_id,
    text,
    search_days=None,
    **kwargs,
):
    # search_days: number of days to search (e.g. 2 for "2 days").
    # **kwargs: swallows any other new argument the caller may add.

    provider = getattr(bot, "SPORTYBET_PROVIDER", None)

    if provider is None:
        return _orig_flow(chat_id, text)

    try:
        req = bot.parse_request(text)

    except Exception:
        return _orig_flow(chat_id, text)

    # Apply the requested search window and fix the header label.
    if search_days:
        try:
            days = int(search_days)
            req["end"] = req["start"] + timedelta(days=days)
            req["label"] = f"next {days} day{'s' if days > 1 else ''}"
        except Exception:
            pass

    target = req.get("target_odds")
    count = req.get("picks")
    risk = req.get("risk") or "normal"

    if not target and not count:
        count = 5

    # No backend progress message is sent to Telegram.
    # The user only receives the final result.

    try:

        built = build_ticket(
            provider,
            req,
            target,
            count,
            risk,
            notify=None,
        )

    except Exception as exc:

        print(f"Smart ticket failed: {exc}")

        return _orig_flow(chat_id, text)

    chosen = built["chosen"]

    local_now = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ)

    if not chosen:

        bot.send_message(
            chat_id,
            (
                "❌ I couldn't build a "
                "football-evidence-backed "
                "ticket from the available "
                "matches for that request."
            ),
        )

        return

    # ========================================================
    # AI NEWS REVIEW
    # ========================================================

    reasons = {}
    swapped = []

    if USE_AI_REVIEW and ANTHROPIC_API_KEY:

        reasons = ai_review(chosen, local_now)

        drops = [
            candidate
            for candidate in chosen
            if reasons.get(
                (candidate["event_id"], candidate["key"]),
                ("", "keep"),
            )[1] == "drop"
        ][:MAX_DROPS]

        if drops:

            swapped = [
                (
                    candidate,
                    reasons[
                        (candidate["event_id"], candidate["key"])
                    ][0],
                )
                for candidate in drops
            ]

            exclude = frozenset(
                (candidate["event_id"], candidate["key"])
                for candidate in drops
            )

            try:

                rebuilt = build_ticket(
                    provider,
                    req,
                    target,
                    count,
                    risk,
                    exclude=exclude,
                )

                if rebuilt["chosen"]:
                    built = rebuilt
                    chosen = rebuilt["chosen"]

            except Exception as exc:

                print(f"News rebuild failed: {exc}")

                swapped = []

    # ========================================================
    # FINAL CALCULATIONS
    # ========================================================

    import football_data as fd

    chosen.sort(key=lambda c: c["kickoff"])

    total_odds = 1.0
    chance = 1.0

    for candidate in chosen:

        total_odds *= candidate["odd"]
        chance *= candidate["p"]

        candidate["reason"] = reasons.get(
            (candidate["event_id"], candidate["key"]),
            (
                fd.reason_for(
                    candidate,
                    candidate.get("facts"),
                    candidate["home"],
                    candidate["away"],
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
                    candidate["event"],
                    {"resolved_key": candidate["key"]},
                )
                for candidate in chosen
            ]
        )

    except Exception as exc:

        print(f"Booking code failed: {exc}")

    # ========================================================
    # TELEGRAM MESSAGE
    # ========================================================

    today = local_now.date()

    title = (
        f"{_fmt(target)} ODDS"
        if target
        else f"{len(chosen)} PICKS"
    )

    lines = [
        (
            "🎯 <b>SPORTYTIPS - "
            f"{html.escape(title)}</b>"
        ),
        (
            f"📅 {html.escape(req['label'].capitalize())}"
            f" • times in {bot.LOCAL_TZ_NAME}"
        ),
    ]

    for number, candidate in enumerate(chosen, start=1):

        lines.append("")

        lines.append(
            f"<b>{number}.</b> "
            f"🕒 {html.escape(_time_text(candidate, today))}"
            f" • 🏆 {html.escape(candidate['league'])}"
        )

        lines.append(
            f"⚽ {html.escape(candidate['home'])}"
            f" vs "
            f"{html.escape(candidate['away'])}"
        )

        lines.append(
            f"✅ <b>{html.escape(candidate['label'])}</b>"
            f" • 💰 {candidate['odd']:.2f}"
            f" • {_confidence(candidate['p'])}"
            f" {round(candidate['p'] * 100)}%"
        )

        lines.append(f"💬 {html.escape(candidate['reason'])}")

    lines += [
        "",
        "━━━━━━━━━━━━",
        f"💰 <b>Total odds: {total_odds:.2f}</b>",
        (
            "📊 Estimated combined chance: "
            f"<b>{chance * 100:.1f}%</b>"
        ),
    ]

    if target and not built["reached"]:

        lines.append(
            (
                f"⚠️ I could not safely reach "
                f"{_fmt(target)} odds with the "
                "available football selections."
            )
        )

    if built["note"]:

        lines.append(f"ℹ️ {html.escape(built['note'])}")

    for candidate, why in swapped:

        lines.append(
            (
                "🔁 Swapped "
                f"{html.escape(candidate['home'])} "
                "vs "
                f"{html.escape(candidate['away'])}: "
                f"{html.escape(why)}"
            )
        )

    mix = _kind_counts(chosen)

    lines.append(
        "🧩 Mix: "
        + ", ".join(
            f"{number} {KIND_NAME.get(kind, kind).lower()}"
            for kind, number in sorted(
                mix.items(),
                key=lambda x: -x[1],
            )
        )
    )

    if code:

        lines.append(
            (
                "📲 SportyBet code: "
                f"<b>{html.escape(str(code))}</b>"
            )
        )

    else:

        lines.append(
            "⚠️ SportyBet booking code could not be created."
        )

    if ANTHROPIC_API_KEY and USE_AI_REVIEW:

        lines.append(
            (
                "💬 Team-news checks are also "
                "used when available. Check "
                "confirmed line-ups before kickoff."
            )
        )

    if len(chosen) >= 12 and target and target >= 50:

        lines.append(
            (
                "⚠️ Big-odds tickets are naturally "
                "high risk. Stake responsibly."
            )
        )

    lines.append(
        "⚠️ Predictions are estimates, not guarantees (18+)."
    )

    bot.send_message(chat_id, "\n".join(lines))

    # ========================================================
    # TICKET IMAGE
    # ========================================================

    try:

        import ticket_image_lite

        rows = [
            {
                "time": _time_text(candidate, today),
                "league": candidate["league"],
                "match": (
                    f"{candidate['home']} "
                    f"vs "
                    f"{candidate['away']}"
                ),
                "pick": candidate["label"],
                "odd": candidate["odd"],
                "prob": candidate["p"],
            }
            for candidate in chosen
        ]

        png = ticket_image_lite.make_ticket_image(
            rows,
            (
                f"{_fmt(target)} Odds"
                if target
                else f"{len(chosen)} Picks"
            ),
            req["label"].capitalize(),
            total_odds,
            chance,
            code,
        )

        _launcher_send_photo(chat_id, png, "")

    except Exception as exc:

        print(f"Ticket picture failed: {exc}")


# ============================================================
# INSTALL THE FLOW
# ============================================================

_orig_flow = bot.prediction_ticket_flow

bot.prediction_ticket_flow = flow

bot.MAX_DAYS_AHEAD = max(
    getattr(bot, "MAX_DAYS_AHEAD", 2),
    3,
)
