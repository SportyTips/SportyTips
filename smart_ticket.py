l"""Smart ticket builder for SportyTips.

Replaces the bot's ticket flow when SportyBet mode is on (USE_SPORTYBET=1).

What it does:
  * reads SportyBet's OWN match list (hundreds of matches, SportyBet's own odds)
  * can reach big targets: 50 odds ~ 50, 100 odds ~ 100 (up to 30 picks)
  * mixes markets: 1UP/2UP wins, team goals, corners, handicap, BTTS, DNB, either half, over goals
  * writes a short reason under every pick (odds-based, no external API)

Market rules:
  * NO Match Winner (1X2) -- 1UP/2UP used instead, drop match if not offered
  * NO Under picks anywhere
  * NO negative handicaps (only +lines)
  * NO Team 2+ in a row (only "Any Team 3+ in a row" -> NO side)
  * DC is 12 only (Home or Away)
  * Corners: 6.5 / 7.5 whole match + 3.5 1st half only, odds between 1.30 and 1.50
  * Team goals: 0.5 / 1.5 only, Over side
  * BTTS: both sides, floor 1.60
  * All other markets: floor 1.30
"""

import html
import importlib
import math
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

import main as bot
import sportybet_provider as sp

BRAND = "SPORTYTIPS"

# ------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------
MIN_LEG_PROB = {"safe": 0.72, "normal": 0.62, "risky": 0.50}
MAX_LEG_ODDS = {"safe": 1.90, "normal": 2.40, "risky": 3.50}
MAX_LEGS = 60
MAX_DETAIL_EVENTS = 80
DETAIL_WORKERS = 10
DETAIL_SECONDS = 90
OVERSHOOT = 0.06
GROUP_LIMIT = 400
DATA_BONUS = 0.0
USE_AI_REVIEW = False
WEB_SEARCHES = 0
MAX_DROPS = 0

# Straight-win (1UP/2UP) mode uses looser probability limits because
# 1UP/2UP odds are mostly 1.30 - 2.50.
STRAIGHT_MIN_PROB = 0.40
STRAIGHT_MAX_ODDS = 2.60

# Per-kind floors
MIN_ODDS = {
    "up": 1.30,
    "dc": 1.30,
    "over15": 1.30,
    "over": 1.30,
    "under": 99.0,           # never
    "btts": 1.60,
    "team_goals": 1.30,
    "streak": 1.30,
    "corners": 1.30,
    "corners_1h": 1.30,
    "dnb": 1.30,
    "handicap": 1.30,
    "asian_handicap": 1.30,
    "either_half": 1.30,
}
MAX_ODDS_KIND = {
    "corners": 1.50,
    "corners_1h": 1.50,
}
ALLOW_DC_12 = True
ALLOW_DC_1X = False
ALLOW_DC_X2 = False
ALLOW_TEAM_GOALS = True
ALLOW_CORNERS = True
ALLOW_STREAK = True
ALLOW_HANDICAP_POSITIVE = True
ALLOW_HANDICAP_NEGATIVE = False

KIND_BONUS = {"up": 0.04, "team_goals": 0.04, "corners": 0.03, "corners_1h": 0.03,
              "handicap": 0.03, "asian_handicap": 0.03, "either_half": 0.03,
              "btts": 0.02, "dnb": 0.02, "streak": 0.03}
LEG_PENALTY = 0.06
KIND_CAP = {
    "over15": 0.20, "over": 0.30, "under": 0.0, "btts": 0.30, "dc": 0.40,
    "up": 0.40, "either_half": 0.25, "corners": 0.30, "corners_1h": 0.20,
    "handicap": 0.30, "asian_handicap": 0.25, "dnb": 0.30,
    "team_goals": 0.35, "streak": 0.25,
}
KIND_NAME = {
    "over15": "Over 1.5", "over": "Over goals", "under": "Under goals",
    "btts": "Both teams to score", "dc": "Double chance", "up": "1UP / 2UP",
    "either_half": "Win either half", "corners": "Corners",
    "corners_1h": "1st half corners", "handicap": "Handicap",
    "asian_handicap": "Asian handicap", "dnb": "Draw no bet",
    "team_goals": "Team goals", "streak": "Team 3+ streak (No)",
}

UP_RE = re.compile(r"1x2\W+([12])\s*-?\s*up\b", re.I)
UP_LOOSE_RE = re.compile(r"\b([12])\s*-?\s*up\b", re.I)


# ------------------------------------------------------------
# PICKS FROM ONE MATCH
# ------------------------------------------------------------
def _f(value):
    return sp._float(value)


def _two_way(odd, other):
    if not odd:
        return None
    if other:
        a, b = 1 / odd, 1 / other
        return a / (a + b)
    return min(0.95 / odd, 0.97)


def _single(odd):
    return min(0.95 / odd, 0.97)


def _side_of(outcome, two_way=False):
    label = str(outcome.get("desc") or outcome.get("name") or "").strip().lower()
    oid = str(outcome.get("id"))
    if label.startswith("home"):
        return "home"
    if label.startswith("away"):
        return "away"
    if label.startswith("draw"):
        return None
    if oid == "1":
        return "home"
    if oid == "3" or (two_way and oid == "2"):
        return "away"
    return None


def _up_candidates(markets, home, away, add):
    """1UP / 2UP win picks (the market named like '1X2 - 1UP' / '1X2 - 2UP')."""
    for market in markets or []:
        text = f"{market.get('desc') or ''} {market.get('name') or ''}"
        found = UP_RE.search(text) or UP_LOOSE_RE.search(text)
        if not found:
            continue
        outs = market.get("outcomes", [])
        if len(outs) != 3:          # real 1UP/2UP markets are 3-way
            continue
        n = int(found.group(1))
        spec = market.get("specifier") or ""
        for o in outs:
            if o.get("isActive") is False:
                continue
            side = _side_of(o)
            if side not in ("home", "away"):
                continue
            odd = _f(o.get("odds"))
            if not odd:
                continue
            team = home if side == "home" else away
            add("up", f"{team} to win ({n}UP)", odd, _single(odd),
                (str(market.get("id")), spec, str(o.get("id"))),
                side=side, up_n=n)


def event_candidates(event, markets):
    """All picks this match offers: dicts with kind, label, odd, p, key."""
    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")
    out = []

    def add(kind, label, odd, p, key, **extra):
        if odd and p and odd > 1:
            item = {"kind": kind, "label": label, "odd": odd, "p": p, "key": key}
            item.update(extra)
            out.append(item)

    ph = pa = None
    h = d = a = None

    # --- 1X2 -> used only for market probabilities (no direct pick)
    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))
    if h and d and a:
        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)
        ph, pd, pa = (x / total for x in inv)

    # --- 1UP / 2UP wins
    _up_candidates(markets, home, away, add)

    # --- double chance (12 only)
    if ALLOW_DC_12:
        k12 = (sp.M_DC, "", sp.OUT_DC["12"])
        if ph is not None and pa is not None:
            add("dc", f"{home} or {away}", sp.find_odds(markets, k12), ph + pa, k12, side="home")

    # --- goals (over only) -- 0.5, 1.5, 2.5, 3.5 lines
    for market in markets or []:
        if str(market.get("id")) != sp.M_TOTAL:
            continue
        spec = market.get("specifier") or ""
        if not spec.startswith("total="):
            continue
        line = _f(spec.replace("total=", ""))
        if line is None or (line * 2) % 1 != 0:
            continue
        if line not in (0.5, 1.5, 2.5, 3.5):
            continue
        over = under = None
        for o in market.get("outcomes", []):
            if o.get("isActive") is False:
                continue
            if str(o.get("id")) == sp.OUT_TOTAL["over"]:
                over = _f(o.get("odds"))
            elif str(o.get("id")) == sp.OUT_TOTAL["under"]:
                under = _f(o.get("odds"))
        mid = str(market.get("id"))
        add("over15" if line == 1.5 else "over", f"Over {line:g} goals", over,
            _two_way(over, under), (mid, spec, sp.OUT_TOTAL["over"]), line=line)

    # --- BTTS (both sides)
    yes = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    no = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["no"]))
    add("btts", "Both teams to score", yes, _two_way(yes, no), (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    add("btts", "Both teams NOT to score", no, _two_way(no, yes), (sp.M_BTTS, "", sp.OUT_BTTS["no"]))

    # --- Home/Away Team Total Goals (0.5 and 1.5 only, Over only)
    if ALLOW_TEAM_GOALS:
        for side, mid, team in (("home", sp.M_HOME_TEAM_GOALS, home), ("away", sp.M_AWAY_TEAM_GOALS, away)):
            for market in markets or []:
                if str(market.get("id")) != mid:
                    continue
                spec = market.get("specifier") or ""
                if not spec.startswith("total="):
                    continue
                line = _f(spec.replace("total=", ""))
                if line not in (0.5, 1.5):
                    continue
                over = under = None
                for o in market.get("outcomes", []):
                    if o.get("isActive") is False:
                        continue
                    if str(o.get("id")) == sp.OUT_TEAM_GOALS["over"]:
                        over = _f(o.get("odds"))
                    elif str(o.get("id")) == sp.OUT_TEAM_GOALS["under"]:
                        under = _f(o.get("odds"))
                add("team_goals", f"{team} to score {line:g}+", over,
                    _two_way(over, under), (mid, spec, sp.OUT_TEAM_GOALS["over"]),
                    side=side, line=line)

    # --- Any Team 3+ in a row (NO side only)
    if ALLOW_STREAK:
        for market in markets or []:
            if str(market.get("id")) != sp.M_STREAK_3:
                continue
            yes = no = None
            for o in market.get("outcomes", []):
                if o.get("isActive") is False:
                    continue
                if str(o.get("id")) == sp.OUT_STREAK["yes"]:
                    yes = _f(o.get("odds"))
                elif str(o.get("id")) == sp.OUT_STREAK["no"]:
                    no = _f(o.get("odds"))
            if no:
                add("streak", "No team to score 3+ in a row", no,
                    _two_way(no, yes), (sp.M_STREAK_3, "", sp.OUT_STREAK["no"]),
                    side="no")

    # --- Corners (whole match 6.5 / 7.5, 1st half 3.5, Over only, cap 1.50)
    if ALLOW_CORNERS:
        for mid, half, lines in ((sp.M_CORNERS, False, (6.5, 7.5)),
                                  (sp.M_CORNERS_1H, True, (3.5,))):
            for market in markets or []:
                if str(market.get("id")) != mid:
                    continue
                spec = market.get("specifier") or ""
                if not spec.startswith("total="):
                    continue
                line = _f(spec.replace("total=", ""))
                if line not in lines:
                    continue
                over = under = None
                for o in market.get("outcomes", []):
                    if o.get("isActive") is False:
                        continue
                    if str(o.get("id")) == sp.OUT_TOTAL["over"]:
                        over = _f(o.get("odds"))
                    elif str(o.get("id")) == sp.OUT_TOTAL["under"]:
                        under = _f(o.get("odds"))
                kind = "corners_1h" if half else "corners"
                prefix = "1st half Over " if half else "Over "
                add(kind, f"{prefix}{line:g} corners", over,
                    _two_way(over, under), (mid, spec, sp.OUT_TOTAL["over"]),
                    line=line, half=half)

    # --- Draw No Bet
    for market in markets or []:
        if str(market.get("id")) != sp.M_DNB:
            continue
        outs = [o for o in market.get("outcomes", []) if o.get("isActive") is not False]
        if len(outs) != 2:
            continue
        odds = [_f(o.get("odds")) for o in outs]
        for o, odd, other in ((outs[0], odds[0], odds[1]), (outs[1], odds[1], odds[0])):
            side = _side_of(o, two_way=True)
            if side and odd:
                team = home if side == "home" else away
                add("dnb", f"{team} (draw no bet)", odd, _two_way(odd, other),
                    (sp.M_DNB, "", str(o.get("id"))), side=side)

    # --- Handicap (positive only) and Asian Handicap (positive only)
    for market in markets or []:
        mid = str(market.get("id"))
        if mid not in (sp.M_HANDICAP, sp.M_ASIAN_HANDICAP):
            continue
        spec = market.get("specifier") or ""
        if not spec.startswith("hcp="):
            continue
        try:
            line = float(spec.replace("hcp=", "").split(":")[0]) if ":" not in spec else float(spec.replace("hcp=", "").split(":")[1])
        except (ValueError, IndexError):
            continue
        outs = [o for o in market.get("outcomes", []) if o.get("isActive") is not False]
        kind = "asian_handicap" if mid == sp.M_ASIAN_HANDICAP else "handicap"
        for o in outs:
            side = _side_of(o)
            if side not in ("home", "away"):
                continue
            odd = _f(o.get("odds"))
            if not odd:
                continue
            label = str(o.get("desc") or o.get("name") or "")
            # positive handicap: the team is GIVEN goals (label has "+")
            if "+" not in label:
                continue
            team = home if side == "home" else away
            add(kind, f"{team} {label.strip()}", odd, _single(odd),
                (mid, spec, str(o.get("id"))), side=side)

    return out


def favourite_strength(event):
    markets = event.get("markets") or []
    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))
    if not (h and d and a):
        return None
    inv = [1 / h, 1 / d, 1 / a]
    total = sum(inv)
    return max(inv[0], inv[2]) / total


# ------------------------------------------------------------
# GATHER
# ------------------------------------------------------------
def gather(provider, start, end, risk, floor, exclude,
           min_p_shift=0.0, notify=None, straight_only=False):
    events = provider.get_upcoming(start, end)
    rated = []
    for event in events:
        strength = favourite_strength(event)
        if strength is not None:
            rated.append((strength, event))
    rated.sort(key=lambda x: x[0], reverse=True)

    details = {}
    wanted = [event for _, event in rated[:MAX_DETAIL_EVENTS]]
    if wanted:
        pool = ThreadPoolExecutor(DETAIL_WORKERS)
        futures = {pool.submit(provider._event_markets_cached, e["eventId"]): e for e in wanted}
        try:
            for future in as_completed(futures, timeout=DETAIL_SECONDS):
                try:
                    details[futures[future]["eventId"]] = future.result()
                except Exception:
                    pass
        except FutureTimeout:
            print(f"Detail fetch timed out: got {len(details)} of {len(wanted)} matches")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    min_p = MIN_LEG_PROB.get(risk, 0.62) - min_p_shift
    max_odd = MAX_LEG_ODDS.get(risk, 2.40)
    if straight_only:
        min_p = min(min_p, STRAIGHT_MIN_PROB)
        max_odd = max(max_odd, STRAIGHT_MAX_ODDS)

    groups = []
    up_seen = 0
    for event in events:
        markets = details.get(event["eventId"]) or event.get("markets") or []
        fixture = sp.sporty_fixture(event)
        kept, seen = [], set()
        for c in event_candidates(event, markets):
            if c["kind"] == "up":
                up_seen += 1
            if straight_only and c["kind"] != "up":
                continue
            floor_kind = MIN_ODDS.get(c["kind"], 1.30)
            cap_kind = MAX_ODDS_KIND.get(c["kind"])
            if c["odd"] < floor_kind:
                continue
            if cap_kind and c["odd"] > cap_kind:
                continue
            if c["p"] < min_p or c["odd"] > max_odd:
                continue
            if (event["eventId"], c["key"]) in exclude or c["label"] in seen:
                continue
            seen.add(c["label"])
            c.update({
                "event": event, "event_id": event["eventId"],
                "home": event.get("homeTeamName", "Home"),
                "away": event.get("awayTeamName", "Away"),
                "kickoff": datetime.fromtimestamp((event.get("estimateStartTime") or 0) / 1000, tz=timezone.utc),
                "league": fixture["league"]["name"],
            })
            kept.append(c)
        if kept:
            groups.append(kept)

    groups.sort(key=lambda g: max(c["p"] for c in g), reverse=True)
    groups = groups[:GROUP_LIMIT]
    print(f"gather: {len(events)} matches in window, {len(details)} with details, "
          f"{up_seen} 1UP/2UP picks found, {len(groups)} usable matches"
          f"{' (straight win)' if straight_only else ''}")
    return groups, len(events), len(details), 0


# ------------------------------------------------------------
# PICK COMBINATION
# ------------------------------------------------------------
def _dp(groups, target):
    S = 200
    tw = math.ceil(math.log(target) * S)
    wm = max(tw, int(math.log(target * (1 + OVERSHOOT)) * S))
    INF = float("inf")
    dp = [INF] * (wm + 1)
    dp[0] = 0.0
    choices = []
    for group in groups:
        new = dp[:]
        ch = [None] * (wm + 1)
        for oi, c in enumerate(group):
            w = round(math.log(c["odd"]) * S)
            cost = (-math.log(c["p"]) - KIND_BONUS.get(c["kind"], 0.0) + LEG_PENALTY)
            if w <= 0 or w > wm:
                continue
            for x in range(0, wm - w + 1):
                base = dp[x]
                if base == INF:
                    continue
                value = base + cost
                if value < new[x + w]:
                    new[x + w] = value
                    ch[x + w] = (oi, x)
        dp = new
        choices.append(ch)
    best = None
    for x in range(tw, wm + 1):
        if dp[x] < INF and (best is None or dp[x] < dp[best]):
            best = x
    if best is None:
        return None
    picked, x = [], best
    for gi in range(len(groups) - 1, -1, -1):
        step = choices[gi][x]
        if step is not None:
            picked.append(groups[gi][step[0]])
            x = step[1]
    picked.reverse()
    return picked


def _product(picks):
    total = 1.0
    for c in picks:
        total *= c["odd"]
    return total


def _dp_exact(groups, target):
    aim, best = target, None
    for _ in range(6):
        picks = _dp(groups, aim)
        if not picks:
            break
        actual = _product(picks)
        if actual >= target and (best is None or actual < _product(best)):
            best = picks
        if target <= actual <= target * (1 + OVERSHOOT + 0.02):
            return picks
        aim = max(aim * target / actual * 1.004, target)
    return best


def _prune(groups, legs, scale):
    drop = set()
    for kind, share in KIND_CAP.items():
        if share <= 0:
            continue
        cap = max(1, math.ceil(share * legs * scale))
        ranked = sorted((c for g in groups for c in g if c["kind"] == kind),
                        key=lambda c: c["p"], reverse=True)
        drop.update(id(c) for c in ranked[cap:])
    pruned = [[c for c in g if id(c) not in drop] for g in groups]
    return [g for g in pruned if g]


def _kind_counts(chosen):
    counts = {}
    for c in chosen:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return counts


def choose_target(groups, target, caps=True):
    """caps=False is used for straight-win mode, where every pick is the same kind."""
    all_w = sorted(math.log(c["odd"]) for g in groups for c in g)
    if not all_w:
        return [], False
    median_w = all_w[len(all_w) // 2]
    legs = max(3, round(math.log(target) / max(median_w, 0.05)))
    scale = 1.0
    chosen = None
    for _ in range(5):
        chosen = None
        for _ in range(3):
            pruned = _prune(groups, legs, scale) if caps else groups
            chosen = _dp_exact(pruned, target) if pruned else None
            if chosen:
                break
            scale *= 1.6
        if not chosen:
            break
        if not caps:
            return chosen, True
        counts = _kind_counts(chosen)
        if all(counts.get(k, 0) <= max(1, math.ceil(s * len(chosen) * scale))
               for k, s in KIND_CAP.items() if s > 0):
            return chosen, True
        legs = len(chosen)
    if chosen:
        return chosen, True
    best = [max(g, key=lambda c: math.log(c["odd"]) / -math.log(c["p"])) for g in groups]
    best.sort(key=lambda c: -math.log(c["p"]) / math.log(c["odd"]))
    return best[:MAX_LEGS], False


def choose_count(groups, count, caps=True):
    ranked = sorted((c for g in groups for c in g),
                    key=lambda c: c["p"] + KIND_BONUS.get(c["kind"], 0.0),
                    reverse=True)
    chosen, used, counts = [], set(), {}
    for c in ranked:
        if c["event_id"] in used:
            continue
        if caps:
            cap = max(1, math.ceil(KIND_CAP.get(c["kind"], 0.3) * count * 1.6))
            if counts.get(c["kind"], 0) >= cap:
                continue
        chosen.append(c)
        used.add(c["event_id"])
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
        if len(chosen) >= count:
            return chosen, True
    return chosen, False


# ------------------------------------------------------------
# REASONS
# ------------------------------------------------------------
def _reason_for(c):
    """Short, honest, odds-based reason. Never mentions H2H or external data."""
    kind = c["kind"]
    odd = c["odd"]
    home, away = c["home"], c["away"]
    side = c.get("side")

    if kind == "up":
        n = c.get("up_n", 2)
        team = home if side == "home" else away
        return f"{team} to win ({n}UP) at {odd:.2f} — pays out early if they go {n} ahead."
    if kind == "dc":
        return f"{home} or {away} at {odd:.2f} — only a draw loses this one."
    if kind == "over15" or kind == "over":
        line = c.get("line", 1.5)
        return f"Over {line:g} goals at {odd:.2f} — the bookmaker expects goals."
    if kind == "btts":
        if c["label"].lower().startswith("both teams not"):
            return f"Both teams NOT to score at {odd:.2f} — the bookmaker expects one side to stay quiet."
        return f"Both teams to score at {odd:.2f} — goals expected from both sides."
    if kind == "team_goals":
        line = c.get("line", 0.5)
        team = home if side == "home" else away
        return f"{team} to score {line:g}+ at {odd:.2f} — the bookmaker expects them to score."
    if kind == "streak":
        return f"No team to score 3+ in a row at {odd:.2f} — rare to see 3 consecutive goals."
    if kind == "corners":
        line = c.get("line", 7.5)
        return f"Over {line:g} corners at {odd:.2f} — a corner-heavy match expected."
    if kind == "corners_1h":
        line = c.get("line", 3.5)
        return f"1st half Over {line:g} corners at {odd:.2f} — the bookmaker expects early corners."
    if kind == "dnb":
        team = home if side == "home" else away
        return f"{team} draw no bet at {odd:.2f} — stake returned if it ends level."
    if kind in ("handicap", "asian_handicap"):
        return f"{c['label']} at {odd:.2f} — team is given a head start."
    if kind == "either_half":
        team = home if side == "home" else away
        return f"{team} to win either half at {odd:.2f}."
    return f"{c['label']} at {odd:.2f}."


# ------------------------------------------------------------
# BUILD + SEND
# ------------------------------------------------------------
def _fmt(value):
    return str(int(value)) if value == int(value) else f"{value:g}"


def _time_text(c, today):
    local = c["kickoff"].astimezone(bot.LOCAL_TZ)
    text = local.strftime("%I:%M %p").lstrip("0")
    return text if local.date() == today else local.strftime("%a ") + text


def _confidence(p):
    return "🟢" if p >= 0.80 else ("🟡" if p >= 0.70 else "🟠")


def _telegram_send_photo(chat_id, png, caption=""):
    """Direct Telegram sendPhoto (used if the launcher has no send_photo)."""
    if not getattr(bot, "BOT_TOKEN", None):
        raise RuntimeError("no Telegram token")
    if isinstance(png, str):
        with open(png, "rb") as fh:
            png = fh.read()
    boundary = "----sportytips" + str(int(time.time() * 1000))
    parts = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption or "")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                 f"filename=\"ticket.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
    parts.append(png)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    request = Request(
        f"https://api.telegram.org/bot{bot.BOT_TOKEN}/sendPhoto",
        data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urlopen(request, timeout=40) as response:
        response.read()


def _launcher_send_photo(chat_id, png, caption=""):
    """Returns None when the picture was sent, otherwise a short reason."""
    problem, send = None, None
    try:
        launcher = importlib.import_module(os.getenv("LAUNCHER_MODULE", "launcher"))
        send = getattr(launcher, "send_photo", None)
        if send is None:
            problem = "launcher has no send_photo()"
    except Exception as exc:
        problem = f"could not import launcher ({exc})"
    if send:
        try:
            send(chat_id, png, caption)
            return None
        except Exception as exc:
            print("Ticket picture: launcher.send_photo failed:")
            traceback.print_exc()
            problem = f"send_photo failed: {exc}"
    print(f"Ticket picture problem: {problem}. Trying Telegram directly.")
    try:
        _telegram_send_photo(chat_id, png, caption)
        return None
    except Exception as exc:
        print(f"Ticket picture could not be sent: {exc}")
        return problem or str(exc)


def build_ticket(provider, req, target, count, risk,
                 exclude=frozenset(), notify=None, straight_only=False,
                 max_days=None):
    if target:
        floor = max(1.10, min(1.45, target ** (1 / 30)))
    else:
        floor = 1.15
    if risk == "risky":
        floor = max(floor, 1.35)

    extra_days = 0
    note = ""
    hard_limit = max_days if max_days is not None else 30
    while True:
        end = req["end"] + timedelta(days=extra_days)
        groups, total_events, detailed, studied = gather(
            provider, req["start"], end, risk, floor, exclude,
            0.05 if (target and target <= 20) else 0.0, notify,
            straight_only=straight_only)

        def select(pool):
            if not pool:
                return [], False
            if target:
                return choose_target(pool, target, caps=not straight_only)
            return choose_count(pool, count, caps=not straight_only)

        chosen, reached = select(groups)
        if reached or extra_days >= hard_limit:
            break
        if not reached and notify and extra_days == 0:
            notify("extend", None)
        extra_days += 1
    if extra_days and chosen:
        note = f"I extended up to {extra_days} day{'s' if extra_days > 1 else ''} to reach your target."
    return {"chosen": chosen, "reached": reached, "note": note,
            "events": total_events, "detailed": detailed, "floor": floor,
            "days_used": extra_days + 1}


def _reset_straight():
    try:
        import upgrades
        upgrades.STRAIGHT_WIN_ONLY = False
    except Exception:
        pass


def flow(chat_id, text):
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)
    if provider is None:
        return _orig_flow(chat_id, text)

    try:
        req = bot.parse_request(text)
    except Exception:
        return _orig_flow(chat_id, text)

    straight_only = bool(re.search(r"straight\s*-?\s*win", text, re.I))
    straight_today = straight_only and bool(re.search(r"\btoday\b", text, re.I)) \
        and not re.search(r"\blong\b", text, re.I)

    target = req.get("target_odds")
    count = req.get("picks")
    risk = req.get("risk") or "normal"
    if not target and not count:
        count = 5

    wait = "about a minute" if (target or 0) >= 20 else "a few seconds"
    intro = "⏳ Going through SportyBet's matches"
    if straight_only:
        intro = f"⏳ Straight win only{' for ' + _fmt(target) + ' odds' if target else ''}"
    elif target:
        intro += f" for {_fmt(target)} odds"
    bot.send_message(chat_id, f"{intro}. This can take {wait}...")

    if straight_only:
        try:
            import upgrades
            upgrades.STRAIGHT_WIN_ONLY = True
        except Exception:
            pass

    try:
        built = build_ticket(
            provider, req, target, count, risk,
            straight_only=straight_only,
            max_days=0 if straight_today else None,
        )
    except Exception as exc:
        traceback.print_exc()
        _reset_straight()
        bot.send_message(chat_id,
                         f"❌ I could not read SportyBet right now: {html.escape(str(exc)[:200])}\n"
                         "Please try again in a minute.")
        return

    chosen = built["chosen"]
    local_now = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ)
    if not chosen:
        if straight_today:
            bot.send_message(chat_id,
                             "❌ I couldn't find any 1UP / 2UP picks for today. "
                             "Try 'straight long ticket' to extend.")
        else:
            bot.send_message(chat_id,
                             "❌ I could not find strong enough picks on SportyBet in that window.")
        _reset_straight()
        return

    if straight_only and straight_today and target and not built["reached"]:
        actual = _product(chosen)
        bot.send_message(
            chat_id,
            f"❌ Today only reaches about {actual:.1f} odds, not {_fmt(target)}.\n"
            f"Try 'straight long ticket' to extend across days."
        )
        _reset_straight()
        return

    chosen.sort(key=lambda c: c["kickoff"])
    total_odds = 1.0
    chance = 1.0
    for c in chosen:
        total_odds *= c["odd"]
        chance *= c["p"]
        c["reason"] = _reason_for(c)

    code, errors = None, []
    try:
        code = provider.create_booking_code(
            [(c["event"], {"resolved_key": c["key"]}) for c in chosen]
        )
    except Exception as exc:
        errors.append(f"Booking code failed: {exc}")

    today = local_now.date()
    title = f"{_fmt(target)} ODDS" if target else f"{len(chosen)} PICKS"
    if straight_only:
        title = f"STRAIGHT WIN · {title}"
    lines = [f"🎯 <b>{BRAND} — {html.escape(title)}</b>",
             f"📅 {html.escape(req['label'].capitalize())} • times in {bot.LOCAL_TZ_NAME}"]
    for number, c in enumerate(chosen, start=1):
        lines.append("")
        lines.append(f"<b>{number}.</b> 🕒 {html.escape(_time_text(c, today))} • 🏆 {html.escape(c['league'])}")
        lines.append(f"⚽ {html.escape(c['home'])} vs {html.escape(c['away'])}")
        lines.append(f"✅ <b>{html.escape(c['label'])}</b> • 💰 {c['odd']:.2f} • {_confidence(c['p'])} {round(c['p'] * 100)}%")
        lines.append(f"💬 {html.escape(c['reason'])}")
    lines += ["", "━━━━━━━━━━━━", f"💰 <b>Total odds: {total_odds:.2f}</b>",
              f"📊 Estimated chance of all picks winning: <b>{chance * 100:.1f}%</b>"]
    if target and not built["reached"]:
        lines.append(f"⚠️ I could not safely reach {_fmt(target)} odds. This is the closest I found.")
    if built["note"]:
        lines.append(f"ℹ️ {html.escape(built['note'])}")
    mix = _kind_counts(chosen)
    lines.append("🧩 Mix: " + ", ".join(
        f"{n} {KIND_NAME.get(k, k).lower()}" for k, n in sorted(mix.items(), key=lambda x: -x[1])
    ))
    if code:
        lines.append(f"📲 SportyBet code: <b>{html.escape(str(code))}</b>")
    lines.append(f"🔎 Read {built['events']} SportyBet matches across {built['days_used']} day(s).")
    if len(chosen) >= 12 and target and target >= 50:
        lines.append("⚠️ Big odds win rarely, even with strong picks. Stake small.")
    lines.append("⚠️ Predictions are estimates, not guarantees (18+).")
    for error in errors:
        lines.append(f"⚠️ {html.escape(error[:160])}")
    bot.send_message(chat_id, "\n".join(lines))

    try:
        import ticket_image_lite
        rows = [{"time": _time_text(c, today), "league": c["league"],
                 "match": f"{c['home']} vs {c['away']}",
                 "pick": c["label"], "odd": c["odd"], "prob": c["p"]} for c in chosen]
        png = ticket_image_lite.make_ticket_image(
            rows, title, req["label"].capitalize(), total_odds, chance, code)
        problem = _launcher_send_photo(chat_id, png, "")
        if problem:
            bot.send_message(chat_id, "🖼️ Ticket picture unavailable: "
                             + html.escape(str(problem)[:150]))
    except Exception as exc:
        print("Ticket picture failed:")
        traceback.print_exc()
        bot.send_message(chat_id, "🖼️ Ticket picture failed: "
                         + html.escape(f"{type(exc).__name__}: {exc}"[:150]))

    _reset_straight()


# ------------------------------------------------------------
# FASTER MATCH LOADING (all pages at once instead of one by one)
# ------------------------------------------------------------
_LOAD_LOCK = threading.Lock()


def _slim_event(event):
    """Keep only what the bot uses from each match (saves a lot of memory)."""
    slim = {key: event[key] for key in ("eventId", "homeTeamName", "awayTeamName",
                                         "estimateStartTime", "sport") if key in event}
    slim["markets"] = _slim_markets(event.get("markets"))
    return slim


def _fast_load_events(self):
    def fresh():
        return self._events and time.time() - self._events_time < sp.EVENTS_CACHE_SECONDS

    if fresh():
        return self._events

    with _LOAD_LOCK:                      # only one load at a time
        if fresh():
            return self._events

        def fetch(page):
            result = self._request(sp.UPCOMING_PATH, {
                "sportId": "sr:sport:1",
                "marketId": f"{sp.M_1X2},{sp.M_DC},{sp.M_TOTAL},{sp.M_BTTS}",
                "pageSize": 100,
                "pageNum": page,
                "todayGames": "false",
            })
            return [_slim_event(e) for e in sp._walk_events(result.get("data"))]

        pages = {}
        with ThreadPoolExecutor(6) as pool:
            futures = {pool.submit(fetch, p): p for p in range(1, sp.MAX_EVENT_PAGES + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    pages[page] = future.result()
                except Exception as exc:
                    print(f"SportyBet page {page} failed: {exc}")
                    pages[page] = None

        if pages.get(1) is None:
            if self._events:
                print("SportyBet refresh failed, using the older match list.")
                return self._events
            raise sp.SportyBetError("Could not load SportyBet matches (page 1 failed).")

        events, seen_ids = [], set()
        for page in sorted(pages):
            for event in pages[page] or []:
                event_id = event.get("eventId")
                if event_id in seen_ids or sp.is_virtual(event):
                    continue
                seen_ids.add(event_id)
                events.append(event)
        if events:
            self._events, self._events_time = events, time.time()
        return events or self._events


sp.SportyBetProvider._load_events = _fast_load_events


# Each match has 100+ markets; keep only the ones the bot uses, so the server
# does not run out of memory on a small Render plan.
KEEP_MARKET_IDS = {sp.M_1X2, sp.M_DC, sp.M_TOTAL, sp.M_BTTS, sp.M_HOME_TEAM_GOALS,
                   sp.M_AWAY_TEAM_GOALS, sp.M_CORNERS, sp.M_CORNERS_1H, sp.M_STREAK_3,
                   sp.M_DNB, sp.M_HANDICAP, sp.M_ASIAN_HANDICAP}
MAX_CACHED_MATCHES = 300


def _slim_markets(markets):
    kept = []
    for market in markets or []:
        text = f"{market.get('desc') or ''} {market.get('name') or ''}"
        if str(market.get("id")) in KEEP_MARKET_IDS or UP_LOOSE_RE.search(text):
            kept.append(market)
    return kept


def _slim_markets_cached(self, event_id):
    hit = self._up_cache.get(event_id)
    if hit and time.time() - hit[0] < sp.UP_MARKETS_CACHE_SECONDS:
        return hit[1]
    markets = _slim_markets(self.get_event_markets(event_id))
    self._up_cache[event_id] = (time.time(), markets)
    if len(self._up_cache) > MAX_CACHED_MATCHES:
        oldest = sorted(self._up_cache, key=lambda k: self._up_cache[k][0])
        for key in oldest[:len(self._up_cache) - MAX_CACHED_MATCHES]:
            self._up_cache.pop(key, None)
    return markets


sp.SportyBetProvider._event_markets_cached = _slim_markets_cached


def _start_warmer():
    """Keeps the match list fresh in the background so tickets start fast."""
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)
    if provider is None:
        return

    def loop():
        while True:
            try:
                provider._events_time = 0
                provider._load_events()
            except Exception as exc:
                print(f"Background refresh failed: {exc}")
            time.sleep(max(60, sp.EVENTS_CACHE_SECONDS - 30))

    threading.Thread(target=loop, daemon=True, name="sportybet-warmer").start()


_orig_flow = bot.prediction_ticket_flow
bot.prediction_ticket_flow = flow
bot.MAX_DAYS_AHEAD = max(getattr(bot, "MAX_DAYS_AHEAD", 2), 3)
_start_warmer()
