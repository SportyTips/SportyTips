"""
football_boost.py - better football evidence for SportyTips.

Put this file next to main.py.
In app.py add   import football_boost   right AFTER the line that imports upgrades.

What it does (it patches football_data.py and smart_ticket.py at import time):
1. Studies EVERY match (no 6-match cap). A time budget is the only limit.
2. Last 10 results per team, cached and reused across matches.
3. Stronger model: recency weights, home/away split, shrinkage, Dixon-Coles
   style draw fix, 1UP/2UP as "team ever leads by 1 or 2".
4. Final chance = football model blended with SportyBet's own odds.
5. Reasons such as "Arsenal won 5 of their last 5 (WWWWW)" on every pick.
6. AI review sees the football data and only adds current team news.
7. No plain wins, no Over 2.5 or higher.
8. Background warm-up fills the cache slowly so tickets build fast.
"""

import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import football_data as fd
import smart_ticket as st


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_on(name, default="1"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# SETTINGS
# ============================================================

RESULTS_LIMIT = _env_int("FOOTBALL_RESULTS", 10)     # recent matches per team
DECAY = 0.88                                          # older games count less
MIN_GAMES = 3                                         # per team, else no facts
MIN_GAP = _env_float("FD_MIN_GAP", 6.5)               # seconds between API calls (free plan = 10/min)
FOOTBALL_WEIGHT = _env_float("FOOTBALL_WEIGHT", 0.6)  # model vs SportyBet odds
MU_PRIOR = 1.35                                       # average goals per team per match
SHRINK = 3.0                                          # pseudo-games pulling towards average
HOME_BOOST = 1.06
AWAY_BOOST = 0.95
DC_RHO = -0.08                                        # draw correction (Dixon-Coles style)
CACHE_TAG = "b1"
WARM_DAYS = _env_int("WARM_DAYS", 7)
WARM_EVERY = _env_int("WARM_EVERY", 600)

fd.ENRICH_MAX = _env_int("ENRICH_MAX", 1000)
fd.ENRICH_SECONDS = _env_int("ENRICH_SECONDS", 150)
st.WEB_SEARCHES = _env_int("WEB_SEARCHES", 8)


# ============================================================
# API PACING (never hammer football-data.org)
# ============================================================

_gate = threading.Lock()
_last_call = [0.0]
_orig_request = fd._football_request


def _paced_request(endpoint, params=None):
    if MIN_GAP > 0:
        with _gate:
            wait = MIN_GAP - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
    return _orig_request(endpoint, params)


fd._football_request = _paced_request


# ============================================================
# RESULTS -> TEAM PROFILE
# ============================================================

def _goals(match):
    score = match.get("score") or {}
    for part in ("regularTime", "fullTime"):
        data = score.get(part) or {}
        if data.get("home") is not None and data.get("away") is not None:
            try:
                return int(data["home"]), int(data["away"])
            except Exception:
                pass
    return None


def _timestamp(match):
    try:
        text = str(match.get("utcDate") or "").replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _team_rows(team_id):
    """Latest finished matches as [timestamp, was_home, goals_for, goals_against].

    The API order is not trusted: we sort by date ourselves.
    A failed request is NEVER cached (the old code cached empty results).
    """
    key = f"team-{CACHE_TAG}:{int(team_id)}"
    cached = fd._cache_get(key)

    if cached:
        return cached

    response = fd._football_request(
        f"teams/{int(team_id)}/matches",
        {"status": "FINISHED"},
    )

    matches = fd._fd4_finished(fd._fd4_list(response))

    if not matches:
        return None

    rows = []

    for match in matches:
        goals = _goals(match)

        if not goals:
            continue

        home_id = ((match.get("homeTeam") or {}).get("id"))
        is_home = 1 if home_id == team_id else 0
        hg, ag = goals

        rows.append([
            _timestamp(match),
            is_home,
            hg if is_home else ag,
            ag if is_home else hg,
        ])

    if not rows:
        return None

    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:RESULTS_LIMIT]

    fd._cache_set(key, rows, fd.FACTS_CACHE_SECONDS)
    return rows


def _split(rows):
    out = {"played": 0, "wins": 0, "draws": 0, "losses": 0, "scored": 0, "conceded": 0}

    for _, _, gf, ga in rows:
        out["played"] += 1
        out["scored"] += gf
        out["conceded"] += ga

        if gf > ga:
            out["wins"] += 1
        elif gf == ga:
            out["draws"] += 1
        else:
            out["losses"] += 1

    return out


def _profile(rows):
    base = _split(rows)
    n = base["played"]

    weights = [DECAY ** i for i in range(n)]
    total = sum(weights) or 1.0

    base["avg_scored"] = sum(w * r[2] for w, r in zip(weights, rows)) / total
    base["avg_conceded"] = sum(w * r[3] for w, r in zip(weights, rows)) / total

    base["form"] = "".join(
        "W" if r[2] > r[3] else "D" if r[2] == r[3] else "L"
        for r in reversed(rows[:5])
    )

    base["home"] = _split([r for r in rows if r[1] == 1])
    base["away"] = _split([r for r in rows if r[1] == 0])

    return base


def _rate(profile, venue, key, mu):
    overall = profile["avg_scored" if key == "for" else "avg_conceded"]
    split = profile[venue]

    if split["played"] >= 2:
        venue_rate = split["scored" if key == "for" else "conceded"] / split["played"]
        overall = 0.7 * overall + 0.3 * venue_rate

    n = profile["played"]
    return (overall * n + mu * SHRINK) / (n + SHRINK)


# ============================================================
# SCORE MODEL
# ============================================================

def _pois(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


@lru_cache(maxsize=1024)
def _matrix_cached(lh, la, size=10):
    ph = [_pois(i, lh) for i in range(size)]
    pa = [_pois(j, la) for j in range(size)]
    m = [[ph[i] * pa[j] for j in range(size)] for i in range(size)]

    m[0][0] *= max(0.0, 1 - lh * la * DC_RHO)
    m[0][1] *= max(0.0, 1 + lh * DC_RHO)
    m[1][0] *= max(0.0, 1 + la * DC_RHO)
    m[1][1] *= max(0.0, 1 - DC_RHO)

    total = sum(sum(row) for row in m) or 1.0
    return tuple(tuple(v / total for v in row) for row in m)


def _sum(m, cond):
    return sum(
        m[i][j]
        for i in range(len(m))
        for j in range(len(m))
        if cond(i, j)
    )


@lru_cache(maxsize=1024)
def _ever_lead(lh, la, k, minutes=95):
    """Chance a team EVER leads by k goals (this is what 1UP / 2UP pays on)."""
    ph = 1 - math.exp(-lh / minutes)
    pa = 1 - math.exp(-la / minutes)

    dist = {0: 1.0}
    hit = 0.0

    for _ in range(minutes):
        new = {}

        for diff, p in dist.items():
            for dh, pr_h in ((1, ph), (0, 1 - ph)):
                for da, pr_a in ((1, pa), (0, 1 - pa)):
                    pr = p * pr_h * pr_a
                    nd = diff + dh - da

                    if nd >= k:
                        hit += pr
                    else:
                        nd = max(nd, -8)
                        new[nd] = new.get(nd, 0.0) + pr

        dist = new

    return min(1.0, hit)


# ============================================================
# FACTS
# ============================================================

def get_facts(fixture):
    if not fixture:
        return None

    home_team, home = fd._fd4_team(fixture, "home")
    away_team, away = fd._fd4_team(fixture, "away")

    hid, aid = home_team.get("id"), away_team.get("id")

    if not hid or not aid:
        return None

    fid = fd._fd4_fixture_id(fixture)
    key = f"facts-{CACHE_TAG}:{fid or hid}-{aid}"

    cached = fd._cache_get(key)

    if cached:
        return cached

    hrows = _team_rows(hid)
    arows = _team_rows(aid)

    if (
        not hrows
        or not arows
        or len(hrows) < MIN_GAMES
        or len(arows) < MIN_GAMES
    ):
        return None

    hp = _profile(hrows)
    ap = _profile(arows)

    count = len(hrows) + len(arows)
    goals = sum(r[2] for r in hrows) + sum(r[2] for r in arows)
    mu = max(0.9, min(2.0, (goals + MU_PRIOR * 4) / (count + 4)))

    atk_h = _rate(hp, "home", "for", mu)
    def_h = _rate(hp, "home", "against", mu)
    atk_a = _rate(ap, "away", "for", mu)
    def_a = _rate(ap, "away", "against", mu)

    lh = max(0.2, min(3.8, HOME_BOOST * atk_h * def_a / mu))
    la = max(0.2, min(3.8, AWAY_BOOST * atk_a * def_h / mu))

    m = _matrix_cached(round(lh, 3), round(la, 3))

    facts = {
        "home": home,
        "away": away,
        "home_id": hid,
        "away_id": aid,
        "fixture_id": fid,
        "competition": (fixture.get("competition") or {}).get("name"),
        "recent": {"home": hp, "away": ap},
        "venue": {"home": hp["home"], "away": ap["away"]},
        "prediction": {"home": None, "draw": None, "away": None},
        "h2h": {"played": 0, "home_wins": 0, "draws": 0, "away_wins": 0},
        "lambda": {"home": lh, "away": la},
        "sample": len(hrows) + len(arows),
        "model": {
            "home": _sum(m, lambda i, j: i > j),
            "draw": _sum(m, lambda i, j: i == j),
            "away": _sum(m, lambda i, j: j > i),
            "over15": _sum(m, lambda i, j: i + j > 1.5),
            "btts": _sum(m, lambda i, j: i >= 1 and j >= 1),
        },
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }

    fd._cache_set(key, facts, fd.FACTS_CACHE_SECONDS)
    return facts


# ============================================================
# PROBABILITY OF EACH PICK
# ============================================================

_BLOCKED_KINDS = {"under", "dnb", "dc", "dc12", "yellow_cards", "cards"}


def _line_of(candidate):
    line = candidate.get("line")

    if line is not None:
        try:
            return float(line)
        except Exception:
            pass

    found = re.findall(r"(\d+(?:\.\d+)?)", str(candidate.get("label") or ""))

    try:
        return float(found[-1]) if found else None
    except Exception:
        return None


def adjusted_p(candidate, facts):
    if not candidate or not facts:
        return 0.0

    kind = str(candidate.get("kind") or "").lower()
    label = str(candidate.get("label") or "").lower()

    if (
        kind in _BLOCKED_KINDS
        or "under " in label
        or "draw no bet" in label
        or "yellow" in label
    ):
        return 0.0

    lam = facts.get("lambda") or {}
    lh, la = lam.get("home"), lam.get("away")

    if not lh or not la:
        return 0.0

    lh, la = round(float(lh), 3), round(float(la), 3)
    m = _matrix_cached(lh, la)
    side = candidate.get("side")
    model = None

    if kind == "up":
        level = candidate.get("up_level") or (2 if "2up" in label else 1)

        if side == "away":
            model = _ever_lead(la, lh, int(level))
        else:
            model = _ever_lead(lh, la, int(level))

    elif kind == "win":
        model = (
            _sum(m, lambda i, j: j > i)
            if side == "away"
            else _sum(m, lambda i, j: i > j)
        )

    elif kind == "either_half":
        half = _matrix_cached(round(lh / 2, 3), round(la / 2, 3))
        one = (
            _sum(half, lambda i, j: j > i)
            if side == "away"
            else _sum(half, lambda i, j: i > j)
        )
        model = 1 - (1 - one) ** 2

    elif kind in ("over15", "over"):
        line = _line_of(candidate)

        if line is not None:
            model = _sum(m, lambda i, j: i + j > line)

    elif kind == "btts":
        model = _sum(m, lambda i, j: i >= 1 and j >= 1)

    elif kind == "team_goals":
        line = _line_of(candidate)

        if line is not None:
            model = (
                _sum(m, lambda i, j: j > line)
                if side == "away"
                else _sum(m, lambda i, j: i > line)
            )

    elif kind == "handicap":
        h = candidate.get("handicap")

        if h is not None:
            h = float(h)
            model = (
                _sum(m, lambda i, j: (j - i) + h > 0)
                if side == "away"
                else _sum(m, lambda i, j: (i - j) + h > 0)
            )

    # corners and anything else: no football evidence, so no football pick
    if model is None:
        return 0.0

    market = candidate.get("market_p")

    if market is None:
        market = candidate.get("p")

    sample = facts.get("sample") or 0
    weight = FOOTBALL_WEIGHT * min(1.0, sample / 16.0)

    p = model if not market else weight * model + (1 - weight) * market

    return max(0.02, min(0.97, p))


# ============================================================
# REASONS
# ============================================================

def _line(name, record, venue, word):
    n = record.get("played", 0)

    if not n:
        return f"{name}: no recent matches found."

    text = f"{name} won {record.get('wins', 0)} of their last {n}"

    if record.get("form"):
        text += f" ({record['form']})"

    if venue and venue.get("played"):
        text += f", {venue.get('wins', 0)} of {venue['played']} {word}"

    text += (
        f", scoring {record.get('scored', 0) / n:.1f} "
        f"and conceding {record.get('conceded', 0) / n:.1f} per game."
    )

    return text


def reason_for(candidate, facts, home=None, away=None):
    if not facts:
        return "No football data was available for this match."

    home = home or facts.get("home") or "Home"
    away = away or facts.get("away") or "Away"

    kind = str((candidate or {}).get("kind") or "").lower()
    label = str((candidate or {}).get("label") or "").lower()

    if kind == "corners" or "corner" in label:
        return "No corner statistics are available, so this pick rests on SportyBet's price only."

    recent = facts.get("recent") or {}
    venue = facts.get("venue") or {}
    lam = facts.get("lambda") or {}

    h = _line(home, recent.get("home") or {}, venue.get("home"), "at home")
    a = _line(away, recent.get("away") or {}, venue.get("away"), "away")

    xg = ""

    if lam.get("home") and lam.get("away"):
        xg = f" Expected goals {lam['home']:.1f} - {lam['away']:.1f}."

    return f"{h} {a}{xg}"


# ============================================================
# AI REVIEW (current team news, on top of the football data)
# ============================================================

AI_SYSTEM = """
You are the football analyst behind SportyTips.

You receive picks. Each one already comes with football data (recent form,
goals, expected goals). That data is trusted. Do NOT repeat it.

For each pick, search the web for CURRENT team news only: injuries,
suspensions, rotation, motivation, travel, weather, dead rubbers.

Rules:
- Only state things you found or know with confidence. Never invent news.
- If you find nothing useful, write: No notable team news found.
- One short note per pick, under 140 characters.
- Mark "drop" ONLY when confirmed news clearly hurts the pick
  (key player out, heavy rotation, nothing to play for). Otherwise "keep".
- Do not guarantee results.

Reply ONLY with JSON:
{"picks":[{"i":0,"note":"...","flag":"keep"}]}
"""


def ai_review(chosen, local_now):
    lines = []

    for i, c in enumerate(chosen):
        when = c["kickoff"].astimezone(st.bot.LOCAL_TZ).strftime("%a %H:%M")

        lines.append(
            f"{i}. {c['home']} vs {c['away']} ({c['league']}) "
            f"kickoff {when} | pick: {c['label']} | "
            f"football data: {st._reason_for(c)}"
        )

    prompt = (
        f"Today is {local_now.strftime('%A %d %B %Y')} (Nigeria time).\n\n"
        + "\n".join(lines)
    )

    data = st._ai_json(prompt, AI_SYSTEM)
    out = {}

    if not isinstance(data, dict):
        return out

    items = data.get("picks")

    if not isinstance(items, list):
        return out

    for item in items:
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue

        if not (0 <= index < len(chosen)):
            continue

        note = str(item.get("note") or item.get("reason") or "").strip()[:200]

        if not note:
            continue

        flag = "drop" if str(item.get("flag", "")).lower() == "drop" else "keep"

        c = chosen[index]
        combined = f"{st._reason_for(c)} News: {note}"

        out[(c["event_id"], c["key"])] = (combined, flag)

    return out


# ============================================================
# YOUR RULES: no plain wins, no Over 2.5 or higher
# ============================================================

_orig_allowed = st._candidate_is_allowed


def _candidate_is_allowed(kind, label=""):
    if str(kind or "").lower() == "over":
        return False

    return _orig_allowed(kind, label)


_prev_candidates = st.event_candidates


def _event_candidates(event, markets):
    return [c for c in _prev_candidates(event, markets) if c.get("kind") != "win"]


# ============================================================
# STUDY WRAPPER (tells the warm-up job to wait while you wait)
# ============================================================

_FOREGROUND = threading.Event()
_prev_study = st.study


def _study(groups, notify=None):
    _FOREGROUND.set()

    try:
        return _prev_study(groups, notify)
    finally:
        _FOREGROUND.clear()


# ============================================================
# BACKGROUND WARM-UP (fills the cache slowly)
# ============================================================

def _warm_once():
    start = datetime.now(timezone.utc)
    loaded = 0

    for offset in range(0, WARM_DAYS + 1):
        date_string = (start + timedelta(days=offset)).strftime("%Y-%m-%d")

        try:
            fixtures = fd._fd4_fixtures_for(date_string)
        except Exception:
            continue

        for fixture in fixtures or []:
            while _FOREGROUND.is_set():
                time.sleep(3)

            try:
                if get_facts(fixture):
                    loaded += 1
            except Exception as exc:
                print(f"Warm-up skipped a match: {type(exc).__name__}")

    print(f"Football warm-up: {loaded} matches ready")


def _warm_loop():
    time.sleep(20)

    while True:
        try:
            _warm_once()
        except Exception as exc:
            print(f"Warm-up failed: {type(exc).__name__}: {exc}")

        time.sleep(WARM_EVERY)


# ============================================================
# INSTALL
# ============================================================

fd.get_facts = get_facts
fd.adjusted_p = adjusted_p
fd.reason_for = reason_for

st.ai_review = ai_review
st._candidate_is_allowed = _candidate_is_allowed
st.event_candidates = _event_candidates
st.study = _study

if _env_on("FOOTBALL_WARMUP", "1"):
    threading.Thread(target=_warm_loop, name="football-warmup", daemon=True).start()

print("football_boost loaded")