"""Smart ticket builder for SportyTips.

Replaces the bot's ticket flow when SportyBet mode is on.
Reads SportyBet's own match list. Uses only SportyBet data.
No external API. No AI.
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

BRAND = "SPORTYTIPS"

MIN_LEG_PROB = {"safe": 0.72, "normal": 0.62, "risky": 0.50}
MAX_LEG_ODDS = {"safe": 1.90, "normal": 2.40, "risky": 3.50}
MAX_LEGS = 60
MAX_DETAIL_EVENTS = 120
DETAIL_WORKERS = 6
DETAIL_SECONDS = 120
OVERSHOOT = 0.06
GROUP_LIMIT = 400
USE_AI_REVIEW = False
WEB_SEARCHES = 0
MAX_DROPS = 0

MIN_ODDS = {
    "up": 1.30,
    "dc": 1.30,
    "over15": 1.30,
    "over": 1.30,
    "under": 99.0,
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


def event_candidates(event, markets):
    home = event.get("homeTeamName", "Home")
    away = event.get("awayTeamName", "Away")
    out = []

    def add(kind, label, odd, p, key, **extra):
        if odd and p and odd > 1:
            item = {"kind": kind, "label": label, "odd": odd, "p": p, "key": key}
            item.update(extra)
            out.append(item)

    ph = None
    pa = None
    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))
    if h and d and a:
        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)
        ph = inv[0] / total
        pa = inv[2] / total

    if ALLOW_DC_12 and ph is not None and pa is not None:
        k12 = (sp.M_DC, "", sp.OUT_DC["12"])
        add("dc", home + " or " + away, sp.find_odds(markets, k12), ph + pa, k12, side="home")

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
        over = None
        under = None
        for o in market.get("outcomes", []):
            if o.get("isActive") is False:
                continue
            if str(o.get("id")) == sp.OUT_TOTAL["over"]:
                over = _f(o.get("odds"))
            elif str(o.get("id")) == sp.OUT_TOTAL["under"]:
                under = _f(o.get("odds"))
        mid = str(market.get("id"))
        kind = "over15" if line == 1.5 else "over"
        add(kind, "Over " + format(line, "g") + " goals", over,
            _two_way(over, under), (mid, spec, sp.OUT_TOTAL["over"]), line=line)

    yes = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    no = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["no"]))
    add("btts", "Both teams to score", yes, _two_way(yes, no), (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    add("btts", "Both teams NOT to score", no, _two_way(no, yes), (sp.M_BTTS, "", sp.OUT_BTTS["no"]))

    if ALLOW_TEAM_GOALS:
        for side, mid, team in (("home", sp.M_HOME_TEAM_GOALS, home),
                                 ("away", sp.M_AWAY_TEAM_GOALS, away)):
            for market in markets or []:
                if str(market.get("id")) != mid:
                    continue
                spec = market.get("specifier") or ""
                if not spec.startswith("total="):
                    continue
                line = _f(spec.replace("total=", ""))
                if line not in (0.5, 1.5):
                    continue
                over = None
                under = None
                for o in market.get("outcomes", []):
                    if o.get("isActive") is False:
                        continue
                    if str(o.get("id")) == sp.OUT_TEAM_GOALS["over"]:
                        over = _f(o.get("odds"))
                    elif str(o.get("id")) == sp.OUT_TEAM_GOALS["under"]:
                        under = _f(o.get("odds"))
                label = team + " to score " + format(line, "g") + "+"
                add("team_goals", label, over, _two_way(over, under),
                    (mid, spec, sp.OUT_TEAM_GOALS["over"]), side=side, line=line)

    if ALLOW_STREAK:
        for market in markets or []:
            if str(market.get("id")) != sp.M_STREAK_3:
                continue
            yes_odd = None
            no_odd = None
            for o in market.get("outcomes", []):
                if o.get("isActive") is False:
                    continue
                if str(o.get("id")) == sp.OUT_STREAK["yes"]:
                    yes_odd = _f(o.get("odds"))
                elif str(o.get("id")) == sp.OUT_STREAK["no"]:
                    no_odd = _f(o.get("odds"))
            if no_odd:
                add("streak", "No team to score 3+ in a row", no_odd,
                    _two_way(no_odd, yes_odd), (sp.M_STREAK_3, "", sp.OUT_STREAK["no"]),
                    side="no")

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
                over = None
                under = None
                for o in market.get("outcomes", []):
                    if o.get("isActive") is False:
                        continue
                    if str(o.get("id")) == sp.OUT_TOTAL["over"]:
                        over = _f(o.get("odds"))
                    elif str(o.get("id")) == sp.OUT_TOTAL["under"]:
                        under = _f(o.get("odds"))
                kind = "corners_1h" if half else "corners"
                prefix = "1st half Over " if half else "Over "
                add(kind, prefix + format(line, "g") + " corners", over,
                    _two_way(over, under), (mid, spec, sp.OUT_TOTAL["over"]),
                    line=line, half=half)

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
                add("dnb", team + " (draw no bet)", odd, _two_way(odd, other),
                    (sp.M_DNB, "", str(o.get("id"))), side=side)

    for market in markets or []:
        mid = str(market.get("id"))
        if mid not in (sp.M_HANDICAP, sp.M_ASIAN_HANDICAP):
            continue
        spec = market.get("specifier") or ""
        if not spec.startswith("hcp="):
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
            if "+" not in label:
                continue
            team = home if side == "home" else away
            add(kind, team + " " + label.strip(), odd, _single(odd),
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
        with ThreadPoolExecutor(DETAIL_WORKERS) as pool:
            futures = {pool.submit(provider._event_markets_cached, e["eventId"]): e for e in wanted}
            try:
                for future in as_completed(futures, timeout=DETAIL_SECONDS):
                    try:
                        details[futures[future]["eventId"]] = future.result()
                    except Exception:
                        pass
            except FutureTimeout:
                pass

    min_p = MIN_LEG_PROB.get(risk, 0.62) - min_p_shift
    max_odd = MAX_LEG_ODDS.get(risk, 2.40)
    groups = []
    for event in events:
        markets = details.get(event["eventId"]) or event.get("markets") or []
        fixture = sp.sporty_fixture(event)
        kept = []
        seen = set()
        for c in event_candidates(event, markets):
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
            c["event"] = event
            c["event_id"] = event["eventId"]
            c["home"] = event.get("homeTeamName", "Home")
            c["away"] = event.get("awayTeamName", "Away")
            c["kickoff"] = datetime.fromtimestamp((event.get("estimateStartTime") or 0) / 1000, tz=timezone.utc)
            c["league"] = fixture["league"]["name"]
            kept.append(c)
        if kept:
            groups.append(kept)

    groups.sort(key=lambda g: max(c["p"] for c in g), reverse=True)
    groups = groups[:GROUP_LIMIT]
    return groups, len(events), len(details), 0


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
            cost = -math.log(c["p"]) - KIND_BONUS.get(c["kind"], 0.0) + LEG_PENALTY
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
    picked = []
    x = best
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
    aim = target
    best = None
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


def choose_target(groups, target):
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
            pruned = _prune(groups, legs, scale)
            chosen = _dp_exact(pruned, target) if pruned else None
            if chosen:
                break
            scale *= 1.6
        if not chosen:
            break
        counts = _kind_counts(chosen)
        ok = True
        for k, s in KIND_CAP.items():
            if s <= 0:
                continue
            if counts.get(k, 0) > max(1, math.ceil(s * len(chosen) * scale)):
                ok = False
                break
        if ok:
            return chosen, True
        legs = len(chosen)
    if chosen:
        return chosen, True
    best = [max(g, key=lambda c: math.log(c["odd"]) / -math.log(c["p"])) for g in groups]
    best.sort(key=lambda c: -math.log(c["p"]) / math.log(c["odd"]))
    return best[:MAX_LEGS], False


def choose_count(groups, count):
    ranked = sorted((c for g in groups for c in g),
                    key=lambda c: c["p"] + KIND_BONUS.get(c["kind"], 0.0),
                    reverse=True)
    chosen = []
    used = set()
    counts = {}
    for c in ranked:
        if c["event_id"] in used:
            continue
        cap = max(1, math.ceil(KIND_CAP.get(c["kind"], 0.3) * count * 1.6))
        if counts.get(c["kind"], 0) >= cap:
            continue
        chosen.append(c)
        used.add(c["event_id"])
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
        if len(chosen) >= count:
            return chosen, True
    return chosen, False


def _reason_for(c):
    kind = c["kind"]
    odd = c["odd"]
    home = c["home"]
    away = c["away"]
    side = c.get("side")
    if kind == "up":
        n = c.get("up_n", 2)
        team = home if side == "home" else away
        return team + " to win (" + str(n) + "UP) at " + format(odd, ".2f") + " - pays out early."
    if kind == "dc":
        return home + " or " + away + " at " + format(odd, ".2f") + " - only a draw loses."
    if kind in ("over15", "over"):
        line = c.get("line", 1.5)
        return "Over " + format(line, "g") + " goals at " + format(odd, ".2f") + " - goals expected."
    if kind == "btts":
        if c["label"].lower().startswith("both teams not"):
            return "Both teams NOT to score at " + format(odd, ".2f") + "."
        return "Both teams to score at " + format(odd, ".2f") + "."
    if kind == "team_goals":
        line = c.get("line", 0.5)
        team = home if side == "home" else away
        return team + " to score " + format(line, "g") + "+ at " + format(odd, ".2f") + "."
    if kind == "streak":
        return "No team to score 3+ in a row at " + format(odd, ".2f") + " - rare."
    if kind == "corners":
        line = c.get("line", 7.5)
        return "Over " + format(line, "g") + " corners at " + format(odd, ".2f") + "."
    if kind == "corners_1h":
        line = c.get("line", 3.5)
        return "1st half Over " + format(line, "g") + " corners at " + format(odd, ".2f") + "."
    if kind == "dnb":
        team = home if side == "home" else away
        return team + " draw no bet at " + format(odd, ".2f") + "."
    if kind in ("handicap", "asian_handicap"):
        return c["label"] + " at " + format(odd, ".2f") + "."
    if kind == "either_half":
        team = home if side == "home" else away
        return team + " to win either half at " + format(odd, ".2f") + "."
    return c["label"] + " at " + format(odd, ".2f") + "."


def _fmt(value):
    return str(int(value)) if value == int(value) else format(value, "g")


def _time_text(c, today):
    local = c["kickoff"].astimezone(bot.LOCAL_TZ)
    text = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return text
    return local.strftime("%a ") + text


def _confidence(p):
    if p >= 0.80:
        return "G"
    if p >= 0.70:
        return "Y"
    return "O"


def _launcher_send_photo(chat_id, png, caption=""):
    try:
        launcher = importlib.import_module(os.getenv("LAUNCHER_MODULE", "launcher"))
        send = getattr(launcher, "send_photo", None)
        if send:
            send(chat_id, png, caption)
    except Exception as exc:
        print("Ticket picture not sent: " + str(exc))


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

        if not groups:
            chosen, reached = [], False
        elif target:
            chosen, reached = choose_target(groups, target)
        else:
            chosen, reached = choose_count(groups, count)

        if reached or extra_days >= hard_limit:
            break
        extra_days += 1
    if extra_days and chosen:
        note = "Extended up to " + str(extra_days) + " day(s) to reach your target."
    return {"chosen": chosen, "reached": reached, "note": note,
            "events": total_events, "detailed": detailed, "floor": floor,
            "days_used": extra_days + 1}


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
    straight_long = straight_only and bool(re.search(r"\blong\b", text, re.I))

    target = req.get("target_odds")
    count = req.get("picks")
    risk = req.get("risk") or "normal"
    if not target and not count:
        count = 5

    wait = "about a minute" if (target or 0) >= 20 else "a few seconds"
    intro = "Going through SportyBet's matches"
    if straight_only:
        intro = "Straight win only"
        if target:
            intro = intro + " for " + _fmt(target) + " odds"
    elif target:
        intro = intro + " for " + _fmt(target) + " odds"
    bot.send_message(chat_id, intro + ". This can take " + wait + "...")

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
        print("Smart ticket failed: " + str(exc))
        return _orig_flow(chat_id, text)

    chosen = built["chosen"]
    local_now = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ)
    if not chosen:
        if straight_today:
            bot.send_message(chat_id,
                             "Could not find any 1UP / 2UP picks for today. "
                             "Try 'straight long ticket' to extend.")
        else:
            bot.send_message(chat_id, "Could not find strong enough picks in that window.")
        if straight_only:
            try:
                import upgrades
                upgrades.STRAIGHT_WIN_ONLY = False
            except Exception:
                pass
        return

    if straight_only and straight_today and target and not built["reached"]:
        actual = 1.0
        for c in chosen:
            actual *= c["odd"]
        bot.send_message(
            chat_id,
            "Today only reaches about " + format(actual, ".1f") + " odds, not "
            + _fmt(target) + ". Try 'straight long ticket' to extend."
        )
        try:
            import upgrades
            upgrades.STRAIGHT_WIN_ONLY = False
        except Exception:
            pass
        return

    chosen.sort(key=lambda c: c["kickoff"])
    total_odds = 1.0
    chance = 1.0
    for c in chosen:
        total_odds *= c["odd"]
        chance *= c["p"]
        c["reason"] = _reason_for(c)

    code = None
    errors = []
    try:
        code = provider.create_booking_code(
            [(c["event"], {"resolved_key": c["key"]}) for c in chosen]
        )
    except Exception as exc:
        errors.append("Booking code failed: " + str(exc))

    today = local_now.date()
    if target:
        title = _fmt(target) + " ODDS"
    else:
        title = str(len(chosen)) + " PICKS"
    if straight_only:
        title = "STRAIGHT WIN - " + title

    lines = ["<b>" + BRAND + " - " + html.escape(title) + "</b>",
             html.escape(req["label"].capitalize()) + " - times in " + bot.LOCAL_TZ_NAME]
    for number, c in enumerate(chosen, start=1):
        lines.append("")
        lines.append("<b>" + str(number) + ".</b> " + html.escape(_time_text(c, today))
                     + " - " + html.escape(c["league"]))
        lines.append(html.escape(c["home"]) + " vs " + html.escape(c["away"]))
        lines.append("<b>" + html.escape(c["label"]) + "</b> - " + format(c["odd"], ".2f")
                     + " - " + str(round(c["p"] * 100)) + "%")
        lines.append(html.escape(c["reason"]))
    lines.append("")
    lines.append("Total odds: <b>" + format(total_odds, ".2f") + "</b>")
    lines.append("Estimated chance of all winning: <b>" + format(chance * 100, ".1f") + "%</b>")
    if target and not built["reached"]:
        lines.append("Could not safely reach " + _fmt(target) + " odds. This is the closest.")
    if built["note"]:
        lines.append(html.escape(built["note"]))
    mix = _kind_counts(chosen)
    mix_parts = []
    for k, n in sorted(mix.items(), key=lambda x: -x[1]):
        mix_parts.append(str(n) + " " + KIND_NAME.get(k, k).lower())
    lines.append("Mix: " + ", ".join(mix_parts))
    if code:
        lines.append("SportyBet code: <b>" + html.escape(str(code)) + "</b>")
    lines.append("Read " + str(built["events"]) + " SportyBet matches across "
                 + str(built["days_used"]) + " day(s).")
    if len(chosen) >= 12 and target and target >= 50:
        lines.append("Big odds win rarely. Stake small.")
    lines.append("Predictions are estimates, not guarantees (18+).")
    for error in errors:
        lines.append(html.escape(error[:160]))
    bot.send_message(chat_id, "\n".join(lines))

    try:
        import ticket_image_lite
        rows = []
        for c in chosen:
            rows.append({
                "time": _time_text(c, today),
                "league": c["league"],
                "match": c["home"] + " vs " + c["away"],
                "pick": c["label"],
                "odd": c["odd"],
                "prob": c["p"],
            })
        png = ticket_image_lite.make_ticket_image(
            rows, title, req["label"].capitalize(), total_odds, chance, code)
        _launcher_send_photo(chat_id, png, "")
    except Exception as exc:
        print("Ticket picture failed: " + str(exc))

    try:
        import upgrades
        upgrades.STRAIGHT_WIN_ONLY = False
    except Exception:
        pass


_orig_flow = bot.prediction_ticket_flow
bot.prediction_ticket_flow = flow
bot.MAX_DAYS_AHEAD = max(getattr(bot, "MAX_DAYS_AHEAD", 2), 3)
