"""Real football facts for SamuelBet AI.

Uses API-Football to match SportyBet fixtures and collect:
- last-5 form
- last-5 goals
- home/away records
- head-to-head
- API-Football prediction data

Football evidence comes first. SportyBet odds are used only to identify
available markets, not as the football evidence itself.
"""

import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

import main as bot
import sportybet_provider as sp


# ------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------

ENRICH_MAX = int(os.getenv("ENRICH_MAX", "10"))
ENRICH_SECONDS = int(os.getenv("ENRICH_SECONDS", "100"))

# Do not waste API calls just because only a few remain.
MIN_QUOTA = int(os.getenv("MIN_QUOTA", "1"))

FACTS_CACHE_SECONDS = 3 * 3600
FIXTURE_CACHE_SECONDS = 10 * 60

# These are used by the safer-ticket helper.
SURE_P_DATA = 0.78
SURE_P_NODATA = 0.00

_facts_cache = {}
_fixture_index = {}


# ------------------------------------------------------------
# TEAM NAME NORMALISATION
# ------------------------------------------------------------

TEAM_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "manchester united fc": "manchester united",

    "man city": "manchester city",
    "manchester city fc": "manchester city",

    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "tottenham hotspur fc": "tottenham hotspur",

    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers fc": "wolverhampton wanderers",

    "newcastle": "newcastle united",
    "newcastle utd": "newcastle united",
    "newcastle united fc": "newcastle united",

    "west ham": "west ham united",
    "west ham utd": "west ham united",

    "brighton": "brighton and hove albion",
    "brighton hove albion": "brighton and hove albion",
    "brighton and hove albion fc": "brighton and hove albion",

    "forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "nottingham forest fc": "nottingham forest",

    "sheffield utd": "sheffield united",
    "sheffield united fc": "sheffield united",

    "leicester": "leicester city",
    "leicester city fc": "leicester city",

    "ipswich": "ipswich town",
    "ipswich town fc": "ipswich town",

    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint germain fc": "paris saint germain",

    "inter": "inter milan",
    "internazionale": "inter milan",
    "inter milan fc": "inter milan",

    "milan": "ac milan",
    "ac milan fc": "ac milan",

    "roma": "as roma",
    "as roma fc": "as roma",

    "lazio": "ss lazio",
    "ss lazio": "ss lazio",

    "juventus fc": "juventus",

    "bayern munchen": "bayern munich",
    "fc bayern munchen": "bayern munich",
    "bayern munich fc": "bayern munich",

    "dortmund": "borussia dortmund",
    "borussia dortmund fc": "borussia dortmund",

    "sporting lisbon": "sporting cp",
    "sporting lisboa": "sporting cp",
    "sporting cp fc": "sporting cp",

    "psv eindhoven": "psv",
    "psv eindhoven fc": "psv",

    "ajax amsterdam": "ajax",
    "afc ajax": "ajax",

    "porto fc": "fc porto",
    "sl benfica": "benfica",

    "ath madrid": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "atletico madrid fc": "atletico madrid",

    "barca": "barcelona",
    "fc barcelona": "barcelona",

    "real madrid cf": "real madrid",

    "monaco fc": "monaco",
    "as monaco": "monaco",
}


def _normalise_team(value):
    """Turn different team-name styles into a comparable form."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("team") or ""

    value = str(value or "").strip().lower()

    # Remove accents.
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))

    # Replace punctuation with spaces.
    value = re.sub(r"[^a-z0-9]+", " ", value)

    # Common football suffixes.
    value = re.sub(
        r"\b(fc|cf|afc|sc|ac|club|football club)\b",
        " ",
        value,
    )

    value = re.sub(r"\s+", " ", value).strip()

    return TEAM_ALIASES.get(value, value)


def _team_similarity(a, b):
    """Similarity score between two football team names."""
    a = _normalise_team(a)
    b = _normalise_team(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))

        if shorter >= 5 and shorter / max(longer, 1) >= 0.55:
            return 0.96

    ratio = SequenceMatcher(None, a, b).ratio()

    at = set(a.split())
    bt = set(b.split())

    if at and bt:
        overlap = len(at & bt) / len(at | bt)
    else:
        overlap = 0.0

    return max(ratio, overlap)


# ------------------------------------------------------------
# API FOOTBALL FIXTURE HELPERS
# ------------------------------------------------------------

def _fixture_teams(fixture):
    teams = fixture.get("teams") or {}

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    return (
        home.get("name", ""),
        away.get("name", ""),
    )


def _fixture_timestamp(fixture):
    value = (fixture.get("fixture") or {}).get("timestamp")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_kickoff(event):
    value = event.get("estimateStartTime")

    try:
        return datetime.fromtimestamp(
            float(value) / 1000,
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OSError):
        return None


def _fixtures_for(date_string):
    """Get API-Football fixtures for a date, cached briefly."""
    cached = _fixture_index.get(date_string)

    if cached:
        created, fixtures = cached

        if time.time() - created < FIXTURE_CACHE_SECONDS:
            return fixtures

    try:
        fixtures = bot.get_allowed_fixtures_cached(date_string)
    except Exception as exc:
        print(f"Fixture lookup failed for {date_string}: {exc}")
        fixtures = []

    _fixture_index[date_string] = (
        time.time(),
        fixtures or [],
    )

    return fixtures or []


def _candidate_dates(kickoff):
    """Check the kickoff date plus neighbouring UTC/local dates."""
    local_date = kickoff.astimezone(bot.LOCAL_TZ).date()

    dates = []

    for offset in (-1, 0, 1):
        value = local_date + timedelta(days=offset)
        dates.append(value.isoformat())

    return dates


def _match_score(event_home, event_away, fixture):
    """Return team-name score and whether the fixture is reversed."""
    fixture_home, fixture_away = _fixture_teams(fixture)

    normal = (
        _team_similarity(event_home, fixture_home)
        + _team_similarity(event_away, fixture_away)
    ) / 2

    reversed_score = (
        _team_similarity(event_home, fixture_away)
        + _team_similarity(event_away, fixture_home)
    ) / 2

    if reversed_score > normal:
        return reversed_score, True

    return normal, False


# ------------------------------------------------------------
# FIND MATCH
# ------------------------------------------------------------

def find_fixture(event):
    """
    Find the API-Football fixture belonging to a SportyBet event.

    Matching is based primarily on the two team names.
    Kickoff time is used as a secondary check instead of requiring
    an unrealistically small 45-minute window.
    """

    if not event:
        return None

    kickoff = _event_kickoff(event)

    if kickoff is None:
        return None

    event_home = event.get("homeTeamName", "")
    event_away = event.get("awayTeamName", "")

    if not event_home or not event_away:
        return None

    best = None
    best_score = 0.0

    for date_string in _candidate_dates(kickoff):
        fixtures = _fixtures_for(date_string)

        for fixture in fixtures:
            team_score, reversed_match = _match_score(
                event_home,
                event_away,
                fixture,
            )

            # Completely unrelated teams are ignored.
            if team_score < 0.70:
                continue

            fixture_time = _fixture_timestamp(fixture)

            time_score = 0.0

            if fixture_time is not None:
                difference = abs(
                    fixture_time - kickoff.timestamp()
                )

                # Strong time bonus when the kickoff is close.
                if difference <= 30 * 60:
                    time_score = 0.20
                elif difference <= 90 * 60:
                    time_score = 0.16
                elif difference <= 180 * 60:
                    time_score = 0.10
                elif difference <= 6 * 3600:
                    time_score = 0.04
                else:
                    time_score = -0.08

            # Team names matter much more than time.
            combined = (
                team_score * 0.84
                + time_score
            )

            # A very strong exact team match can survive a larger
            # kickoff difference.
            if team_score >= 0.94:
                combined += 0.08

            if combined > best_score:
                best_score = combined
                best = fixture

    if best is None:
        return None

    # Final safety gate.
    home_score = _team_similarity(
        event_home,
        _fixture_teams(best)[0],
    )
    away_score = _team_similarity(
        event_away,
        _fixture_teams(best)[1],
    )

    normal_score = (home_score + away_score) / 2

    if normal_score >= 0.78:
        return best

    # Sometimes SportyBet/API-Football naming can be reversed.
    reverse_home = _team_similarity(
        event_home,
        _fixture_teams(best)[1],
    )
    reverse_away = _team_similarity(
        event_away,
        _fixture_teams(best)[0],
    )

    if (reverse_home + reverse_away) / 2 >= 0.90:
        return best

    return None


# ------------------------------------------------------------
# FACTS
# ------------------------------------------------------------

def _num(value):
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def parse_facts(item):
    """Extract useful football facts from API-Football predictions."""
    if not item:
        return None

    teams = item.get("teams") or {}

    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}

    def form5(team):
        form = ((team.get("league") or {}).get("form")) or ""
        return str(form)[-5:]

    def last5_goal(team, kind):
        node = (
            ((team.get("last_5") or {}).get("goals") or {})
            .get(kind)
            or {}
        )

        return _num(node.get("average"))

    def venue_record(team, venue):
        fixtures = (
            (team.get("league") or {}).get("fixtures")
            or {}
        )

        node = fixtures.get(venue) or {}

        return {
            "played": node.get("played"),
            "w": node.get("wins"),
            "d": node.get("draws"),
            "l": node.get("loses"),
        }

    # API-Football prediction percentages.
    percent = (
        (item.get("predictions") or {}).get("percent")
        or {}
    )

    api = {}

    for key in ("home", "draw", "away"):
        value = _num(percent.get(key))
        api[key] = (value or 0.0) / 100

    # --------------------------------------------------------
    # HEAD TO HEAD
    # --------------------------------------------------------

    home_id = home_t.get("id")

    meetings = list(item.get("h2h") or [])

    meetings.sort(
        key=lambda m: str(
            (m.get("fixture") or {}).get("date")
        ),
        reverse=True,
    )

    rows = []

    for meeting in meetings[:6]:
        goals = meeting.get("goals") or {}

        gh = goals.get("home")
        ga = goals.get("away")

        if gh is None or ga is None:
            continue

        meeting_home = (
            ((meeting.get("teams") or {}).get("home") or {})
            .get("id")
        )

        if meeting_home == home_id:
            rows.append((gh, ga))
        else:
            rows.append((ga, gh))

    h2h = {
        "n": len(rows),
        "home_w": sum(
            1 for a, b in rows
            if a > b
        ),
        "draws": sum(
            1 for a, b in rows
            if a == b
        ),
        "away_w": sum(
            1 for a, b in rows
            if a < b
        ),
        "avg_goals": (
            sum(a + b for a, b in rows) / len(rows)
            if rows else None
        ),
        "btts": sum(
            1 for a, b in rows
            if a > 0 and b > 0
        ),
    }

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    gf_h = last5_goal(home_t, "for")
    ga_h = last5_goal(home_t, "against")

    gf_a = last5_goal(away_t, "for")
    ga_a = last5_goal(away_t, "against")

    lam_h = (
        (gf_h + ga_a) / 2
        if gf_h is not None and ga_a is not None
        else None
    )

    lam_a = (
        (gf_a + ga_h) / 2
        if gf_a is not None and ga_h is not None
        else None
    )

    facts = {
        "form_h": form5(home_t),
        "form_a": form5(away_t),

        "gf_h": gf_h,
        "ga_h": ga_h,
        "gf_a": gf_a,
        "ga_a": ga_a,

        "lam_h": lam_h,
        "lam_a": lam_a,

        "rec_h": venue_record(home_t, "home"),
        "rec_a": venue_record(away_t, "away"),

        "h2h": h2h,
        "api": api,
    }

    has_something = any(
        (
            facts["form_h"],
            facts["form_a"],
            h2h["n"],
            gf_h is not None,
            ga_h is not None,
            gf_a is not None,
            ga_a is not None,
        )
    )

    return facts if has_something else None


# ------------------------------------------------------------
# GET FACTS
# ------------------------------------------------------------

def get_facts(fixture):
    """Get football facts for one fixture."""
    if not fixture:
        return None

    fixture_id = (
        (fixture.get("fixture") or {})
        .get("id")
    )

    if not fixture_id:
        return None

    cached = _facts_cache.get(fixture_id)

    if cached:
        created, facts = cached

        if time.time() - created < FACTS_CACHE_SECONDS:
            return facts

    remaining = getattr(
        bot,
        "_api_remaining",
        None,
    )

    if (
        remaining is not None
        and remaining < MIN_QUOTA
    ):
        return None

    try:
        result = bot.football_request(
            "predictions",
            {"fixture": fixture_id},
        )
    except Exception as exc:
        print(
            f"Facts for fixture {fixture_id} failed: {exc}"
        )
        return None

    response = result.get("response") or []

    facts = (
        parse_facts(response[0])
        if response
        else None
    )

    _facts_cache[fixture_id] = (
        time.time(),
        facts,
    )

    return facts


# ------------------------------------------------------------
# FACT SUMMARY
# ------------------------------------------------------------

def facts_digest(facts):
    """One short football-facts line for the AI reviewer."""
    if not facts:
        return "no head-to-head or last-5 data"

    h2h = facts["h2h"]

    parts = [
        f"last 5 form home {facts['form_h'] or '?'} / "
        f"away {facts['form_a'] or '?'}"
    ]

    if (
        facts["gf_h"] is not None
        and facts["ga_h"] is not None
        and facts["gf_a"] is not None
        and facts["ga_a"] is not None
    ):
        parts.append(
            f"last-5 goals per game: "
            f"home {facts['gf_h']:.1f} for / "
            f"{facts['ga_h']:.1f} against, "
            f"away {facts['gf_a']:.1f} for / "
            f"{facts['ga_a']:.1f} against"
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
                f", {h2h['avg_goals']:.1f} goals a game"
            )

        text += f", both scored {h2h['btts']}"

        parts.append(text)

    rec_h = facts["rec_h"]
    rec_a = facts["rec_a"]

    if rec_h.get("played"):
        parts.append(
            f"home team won {rec_h['w']} "
            f"of {rec_h['played']} at home this season"
        )

    if rec_a.get("played"):
        parts.append(
            f"away team won {rec_a['w']} "
            f"of {rec_a['played']} away this season"
        )

    return "; ".join(parts)


# ------------------------------------------------------------
# POISSON
# ------------------------------------------------------------

def _poisson_cdf(k, lam):
    if lam is None or lam < 0:
        return None

    return sum(
        math.exp(-lam)
        * lam ** i
        / math.factorial(i)
        for i in range(int(k) + 1)
    )


# ------------------------------------------------------------
# FOOTBALL-EVIDENCE PROBABILITY
# ------------------------------------------------------------

def adjusted_p(c, facts):
    """
    Recalculate the candidate using football evidence.

    The original SportyBet probability is treated only as a starting point.
    API-Football form/goals/H2H evidence then adjusts it.
    """

    p = float(c.get("p") or 0.0)

    if not facts:
        return p

    kind = c.get("kind")
    side = c.get("side")

    api = facts.get("api") or {}

    # --------------------------------------------------------
    # WIN / 1UP / 2UP / EITHER HALF
    # --------------------------------------------------------

    if (
        kind in (
            "up",
            "either_half",
            "win",
            "handicap",
        )
        and side in ("home", "away")
    ):
        form = (
            facts.get("form_h")
            if side == "home"
            else facts.get("form_a")
        )

        opp_form = (
            facts.get("form_a")
            if side == "home"
            else facts.get("form_h")
        )

        rec = (
            facts.get("rec_h")
            if side == "home"
            else facts.get("rec_a")
        )

        h2h = facts.get("h2h") or {}

        evidence = []

        if form:
            evidence.append(
                form.count("W") / len(form)
            )

        if opp_form:
            # Opponent losing form helps the selected side.
            evidence.append(
                1 - (
                    opp_form.count("W")
                    / len(opp_form)
                )
            )

        if rec and rec.get("played"):
            try:
                evidence.append(
                    float(rec.get("w") or 0)
                    / float(rec["played"])
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        if h2h.get("n", 0) >= 3:
            wins = (
                h2h["home_w"]
                if side == "home"
                else h2h["away_w"]
            )

            evidence.append(
                wins / h2h["n"]
            )

        # API-Football prediction is football analysis,
        # not bookmaker odds.
        if api.get(side):
            evidence.append(api[side])

        if evidence:
            football_p = sum(evidence) / len(evidence)

            # Blend with the market baseline, but let the football
            # evidence have the stronger influence.
            p = (
                0.35 * p
                + 0.65 * football_p
            )

    # --------------------------------------------------------
    # GOALS
    # --------------------------------------------------------

    lam_h = facts.get("lam_h")
    lam_a = facts.get("lam_a")

    if (
        lam_h is not None
        and lam_a is not None
    ):
        total_lam = lam_h + lam_a

        if (
            kind in (
                "over",
                "over15",
                "under",
            )
            and c.get("line") is not None
        ):
            line = float(c["line"])

            # For total goals > line:
            # P(X >= floor(line)+1)
            cutoff = math.floor(line)

            under_probability = _poisson_cdf(
                cutoff,
                total_lam,
            )

            if under_probability is not None:
                over_probability = (
                    1 - under_probability
                )

                model = (
                    over_probability
                    if kind != "under"
                    else under_probability
                )

                p = (
                    0.30 * p
                    + 0.70 * model
                )

        elif kind == "btts":
            both_score = (
                1 - math.exp(-lam_h)
            ) * (
                1 - math.exp(-lam_a)
            )

            model = (
                both_score
                if c.get("label")
                == "Both teams to score"
                else 1 - both_score
            )

            p = (
                0.30 * p
                + 0.70 * model
            )

    return max(
        0.02,
        min(0.97, p),
    )


# ------------------------------------------------------------
# REASONS
# ------------------------------------------------------------

def _cap(text):
    return (
        text[:1].upper() + text[1:]
        if text
        else text
    )


def _win_like(c, facts, home, away):
    side = c.get("side")

    if side not in ("home", "away"):
        return None

    team, opp = (
        (home, away)
        if side == "home"
        else (away, home)
    )

    form = (
        facts["form_h"]
        if side == "home"
        else facts["form_a"]
    )

    opp_form = (
        facts["form_a"]
        if side == "home"
        else facts["form_h"]
    )

    rec = (
        facts["rec_h"]
        if side == "home"
        else facts["rec_a"]
    )

    h2h = facts["h2h"]

    first = []

    if form:
        first.append(
            f"{team} won {form.count('W')} "
            f"of their last {len(form)} games ({form})"
        )

    if rec and rec.get("played"):
        location = (
            "at home"
            if side == "home"
            else "away"
        )

        first.append(
            f"{location} they have won "
            f"{rec['w']} of {rec['played']} "
            f"this season"
        )

    sentence = (
        _cap(" and ".join(first) + ".")
        if first
        else ""
    )

    second = ""

    if h2h["n"] >= 3:
        mine = (
            h2h["home_w"]
            if side == "home"
            else h2h["away_w"]
        )

        second = (
            f" In the last {h2h['n']} meetings "
            f"with {opp} they won {mine}."
        )

    elif opp_form:
        second = (
            f" {opp} won only "
            f"{opp_form.count('W')} of their "
            f"last {len(opp_form)} "
            f"({opp_form})."
        )

    kind = c["kind"]

    tail = ""

    if kind == "up":
        if "2UP" in c["label"]:
            tail = (
                " The 2UP market needs them to establish "
                "the required two-goal advantage."
            )
        else:
            tail = (
                " The 1UP market gives an early payout "
                "when they establish the required lead."
            )

    elif kind == "either_half":
        tail = (
            " They only need to win one half."
        )

    elif kind == "handicap":
        tail = (
            " The recent form makes the handicap "
            "line worth considering."
        )

    return (
        sentence
        + second
        + tail
    ).strip() or None


def _dc_like(c, facts, home, away):
    side = c.get("side")

    if side not in ("home", "away"):
        return None

    team = (
        home
        if side == "home"
        else away
    )

    form = (
        facts["form_h"]
        if side == "home"
        else facts["form_a"]
    )

    if not form:
        return None

    text = (
        f"{team} lost only "
        f"{form.count('L')} of their "
        f"last {len(form)} games ({form})."
    )

    h2h = facts["h2h"]

    if h2h["n"] >= 3:
        lost = (
            h2h["away_w"]
            if side == "home"
            else h2h["home_w"]
        )

        text += (
            f" They lost {lost} of the last "
            f"{h2h['n']} meetings."
        )

    return text


def _goals_like(c, facts, home, away):
    kind = c["kind"]

    gf_h = facts["gf_h"]
    ga_h = facts["ga_h"]
    gf_a = facts["gf_a"]
    ga_a = facts["ga_a"]

    h2h = facts["h2h"]

    avg = ""

    if (
        h2h["n"] >= 3
        and h2h["avg_goals"] is not None
    ):
        avg = (
            f" The last {h2h['n']} meetings "
            f"averaged {h2h['avg_goals']:.1f} goals."
        )

    if None in (
        gf_h,
        ga_h,
        gf_a,
        ga_a,
    ):
        return None

    if kind in ("over", "over15"):
        return (
            f"{home} score {gf_h:.1f} and "
            f"{away} {gf_a:.1f} goals a game "
            f"over their last 5.{avg}"
        )

    if kind == "under":
        return (
            f"{home} concede {ga_h:.1f} and "
            f"{away} {ga_a:.1f} a game "
            f"over their last 5.{avg}"
        )

    if kind == "btts":
        both = ""

        if h2h["n"] >= 3:
            both = (
                f" Both teams scored in "
                f"{h2h['btts']} of the last "
                f"{h2h['n']} meetings."
            )

        if c["label"] == "Both teams to score":
            return (
                f"{home} score {gf_h:.1f} and "
                f"{away} {gf_a:.1f} a game "
                f"over their last 5.{both}"
            )

        return (
            f"{home} concede {ga_h:.1f} and "
            f"{away} {ga_a:.1f} a game "
            f"over their last 5.{both}"
        )

    if kind == "corners":
        return (
            f"{home} score {gf_h:.1f} and "
            f"{away} {gf_a:.1f} a game "
            f"over their last 5, supporting an "
            f"attacking-game angle."
        )

    return None


def reason_for(c, facts, home, away):
    """Create a short reason using football evidence."""
    if not facts:
        return (
            "There was not enough football data available "
            "to give this pick a proper evidence-based reason."
        )

    kind = c["kind"]

    if kind in (
        "up",
        "either_half",
        "win",
        "handicap",
    ):
        text = _win_like(
            c,
            facts,
            home,
            away,
        )

    elif kind == "dc":
        text = _dc_like(
            c,
            facts,
            home,
            away,
        )

    else:
        text = _goals_like(
            c,
            facts,
            home,
            away,
        )

    return (
        text
        or "Recent form and match data support this selection."
    )


# ------------------------------------------------------------
# TICKETS THE USER PASTES
# ------------------------------------------------------------

def event_for(provider, event_id):
    try:
        events = provider._load_events()
    except Exception:
        return None

    for event in events:
        if event.get("eventId") == event_id:
            return event

    return None


def classify_leg(leg):
    """Classify a pasted SportyBet selection."""
    mid, spec, oid = leg["key"]

    mname = str(
        leg.get("market_name") or ""
    ).lower()

    oname = str(
        leg.get("outcome_name") or ""
    ).lower()

    out = {
        "label": sp.leg_label(leg),
        "side": None,
        "line": None,
    }

    if mid == sp.M_1X2:
        out["kind"] = "win"

        out["side"] = {
            "1": "home",
            "3": "away",
        }.get(oid)

    elif mid == sp.M_DC:
        out["kind"] = "dc"

        out["side"] = {
            "9": "home",
            "11": "away",
        }.get(oid)

    elif mid == sp.M_TOTAL:
        out["line"] = sp._float(
            spec.replace("total=", "")
        )

        out["kind"] = (
            "over"
            if oid == sp.OUT_TOTAL["over"]
            else "under"
        )

    elif mid == sp.M_BTTS:
        out["kind"] = "btts"

    elif "either half" in mname:
        out["kind"] = "either_half"

        out["side"] = (
            "away"
            if "away" in oname
            else "home"
        )

    elif (
        "1x2" in mname
        and "up" in mname
    ):
        out["kind"] = "up"

        out["side"] = (
            "away"
            if (
                oname.startswith("away")
                or oid == "3"
            )
            else "home"
        )

    elif "corner" in mname:
        out["kind"] = "corners"

    elif "handicap" in mname:
        out["kind"] = "handicap"

        out["side"] = (
            "away"
            if (
                oname.startswith("away")
                or oid == "2"
            )
            else "home"
        )

    elif "draw no bet" in mname:
        out["kind"] = "dnb"

        out["side"] = (
            "away"
            if (
                oname.startswith("away")
                or oid == "5"
            )
            else "home"
        )

    else:
        out["kind"] = "other"

    return out


def why(provider, code, index):
    """Return the reason for one pick in a pasted code."""
    legs = provider.load_code(code)

    if not (
        0 <= index < len(legs)
    ):
        raise sp.SportyBetError(
            "That pick is no longer on the ticket."
        )

    leg = legs[index]

    event = event_for(
        provider,
        leg["event_id"],
    )

    facts = None

    if event is not None:
        facts = get_facts(
            find_fixture(event)
        )

    c = classify_leg(leg)

    return {
        "reason": reason_for(
            c,
            facts,
            leg["home"],
            leg["away"],
        ),
        "has_data": bool(facts),
    }


# ------------------------------------------------------------
# RANK PICKS FOR AN EXISTING MATCH
# ------------------------------------------------------------

def _rank(provider, event, use_data):
    """Rank all available markets for one match."""
    import smart_ticket

    try:
        markets = provider._event_markets_cached(
            event["eventId"]
        )
    except Exception:
        markets = None

    markets = (
        markets
        or event.get("markets")
        or []
    )

    facts = (
        get_facts(find_fixture(event))
        if use_data
        else None
    )

    ranked = []

    for c in smart_ticket.event_candidates(
        event,
        markets,
    ):
        c["p"] = adjusted_p(
            c,
            facts,
        )

        c["has_data"] = bool(facts)

        if c["odd"] >= 1.08:
            ranked.append(c)

    ranked.sort(
        key=lambda c: (
            c["p"]
            + 0.1
            * min(
                c["odd"] - 1,
                0.4,
            )
        ),
        reverse=True,
    )

    return ranked, facts


def safest_for_leg(provider, leg):
    """Find a stronger evidence-based selection for the same match."""
    event = event_for(
        provider,
        leg["event_id"],
    )

    if event is None:
        return None

    ranked, facts = _rank(
        provider,
        event,
        True,
    )

    if not ranked or not facts:
        return None

    best = ranked[0]

    current = (
        min(
            0.95 / (leg.get("odd") or 1.0),
            0.97,
        )
        if (leg.get("odd") or 0) > 1
        else 0.97
    )

    if (
        best["key"] == leg["key"]
        or best["p"] <= current
    ):
        return None

    return (
        best["key"],
        best["odd"],
        best["label"],
        reason_for(
            best,
            facts,
            leg["home"],
            leg["away"],
        ),
    )


# ------------------------------------------------------------
# REBUILD A TICKET SAFER
# ------------------------------------------------------------

def rebuild_safer(provider, code):
    """
    Replace each match with its strongest evidence-based market.

    Matches without football data are not treated as 'sure' picks.
    """
    legs = provider.load_code(code)

    started = time.time()

    studied = 0
    new_legs = []
    dropped = []

    for leg in legs:
        event = event_for(
            provider,
            leg["event_id"],
        )

        match = (
            f"{leg['home']} vs "
            f"{leg['away']}"
        )

        if event is None:
            dropped.append({
                "match": match,
                "why": (
                    "this match is no longer "
                    "on SportyBet's list"
                ),
            })
            continue

        use_data = (
            studied < ENRICH_MAX
            and (
                time.time() - started
                < ENRICH_SECONDS
            )
        )

        ranked, facts = _rank(
            provider,
            event,
            use_data,
        )

        if use_data:
            studied += 1

        # No football facts = do not call the pick sure.
        if not facts:
            dropped.append({
                "match": match,
                "why": (
                    "there was not enough football "
                    "data to support a safer pick"
                ),
            })
            continue

        best = (
            ranked[0]
            if ranked
            else None
        )

        if (
            best is None
            or best["p"] < SURE_P_DATA
        ):
            dropped.append({
                "match": match,
                "why": (
                    "no evidence-based pick "
                    "was strong enough"
                ),
            })
            continue

        new_legs.append({
            "event_id": event["eventId"],
            "home": leg["home"],
            "away": leg["away"],
            "key": best["key"],
            "odd": best["odd"],
            "label_override": best["label"],
            "reason": reason_for(
                best,
                facts,
                leg["home"],
                leg["away"],
            ),
        })

    if not new_legs:
        raise sp.SportyBetError(
            "None of those matches had enough "
            "football evidence for a safer ticket."
        )

    new_code = provider._save_code(
        [
            (
                l["event_id"],
                l["key"],
            )
            for l in new_legs
        ]
    )

    result = sp.summarize_legs(
        new_legs
    )

    result.update({
        "code": new_code,
        "changed": True,
        "dropped": dropped,
        "studied": studied,
    })

    return result


# ------------------------------------------------------------
# CACHE CONTROL
# ------------------------------------------------------------

def clear_cache():
    """Clear football-data caches."""
    _facts_cache.clear()
    _fixture_index.clear()


def diag_summary():
    """Small internal diagnostic helper."""
    return {
        "facts_cached": len(_facts_cache),
        "fixture_dates_cached": len(_fixture_index),
        "enrich_max": ENRICH_MAX,
        "enrich_seconds": ENRICH_SECONDS,
    }