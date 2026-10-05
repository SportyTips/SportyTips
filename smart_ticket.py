"""Smart ticket builder for SamuelBet AI.

Replaces the bot's ticket flow when SportyBet mode is on (USE_SPORTYBET=1).

What it does differently from the old flow:
  * reads SportyBet's OWN match list (hundreds of matches, SportyBet's own odds)
  * can reach big targets: 50 odds ~ 50, 100 odds ~ 100 (up to 30 picks)
  * mixes markets: double chance, 1UP / 2UP wins, win either half, draw no bet,
    corners, handicap, both teams to score, other goal lines (Over 1.5 is capped)
  * writes a reason under every pick (the AI adds team news when it can search)

Put it next to main.py. upgrades.py imports it automatically.
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

# ------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------
MIN_LEG_ODDS = 1.30                                             # no pick below this (small odds are not safer)
MIN_LEG_PROB = {"safe": 0.60, "normal": 0.55, "risky": 0.45}   # chance a single pick must have
MAX_LEG_ODDS = {"safe": 1.90, "normal": 2.20, "risky": 3.20}   # biggest odds one pick may have
MAX_LEGS = 30               # SportyBet slips get unreliable above this
MAX_DETAIL_EVENTS = 70      # matches whose full market list is read (corners, handicap, 1UP...)
DETAIL_WORKERS = 6
DETAIL_SECONDS = 45
OVERSHOOT = 0.06            # 100 odds target may end at up to 106
GROUP_LIMIT = 260           # most matches considered at once
DATA_BONUS = 0.12           # matches I studied (last 5 + head-to-head) are preferred
USE_AI_REVIEW = os.getenv("USE_AI_REVIEW", "1") == "1"   # set USE_AI_REVIEW=0 on Render for the fastest tickets
_UNUSED_AI_FLAG = True        # AI writes reasons and checks team news
WEB_SEARCHES = 6            # most web searches per ticket (news + results for matches without data)
AI_SECONDS = 55             # the AI step never takes longer than this
SKIP_AI_AFTER = 140         # skip the AI step if the ticket already took this long
WARM_UP = os.getenv("WARM_UP", "1") == "1"
MAX_DROPS = 4               # picks the AI may swap out because of news

# What each ticket type may use
MODE_KINDS = {
    "mixed": {"up", "either_half", "dnb", "corners", "handicap", "btts", "over", "over15", "under"},
    "up2": {"up"}, "up1": {"up"}, "win": {"win"}, "dc": {"dc"},
}
MODE_NAME = {"up2": "2UP wins", "up1": "1UP wins", "win": "Straight wins", "dc": "Double chance", "mixed": ""}

# Small nudges so the ticket uses the markets you asked for and does not use more picks than needed
KIND_BONUS = {"up": 0.04, "corners": 0.04, "handicap": 0.04, "either_half": 0.03, "btts": 0.02, "dnb": 0.02}
LEG_PENALTY = 0.06

# Share of the ticket one kind of pick may take (Over 1.5 is kept small on purpose)
KIND_CAP = {
    "over15": 0.20, "over": 0.30, "under": 0.30, "btts": 0.25, "dc": 0.40,
    "up": 0.40, "either_half": 0.25, "corners": 0.30, "handicap": 0.30, "dnb": 0.30,
}
KIND_NAME = {
    "over15": "Over 1.5", "over": "Over goals", "under": "Under goals", "btts": "Both teams to score",
    "dc": "Double chance", "up": "1UP / 2UP", "either_half": "Win either half",
    "corners": "Corners", "handicap": "Handicap", "dnb": "Draw no bet",
}


# ------------------------------------------------------------
# PICKS FROM ONE MATCH
# ------------------------------------------------------------
def _f(value):
    return sp._float(value)


def _two_way(odd, other):
    """Chance with the bookmaker margin removed (two outcomes)."""
    if not odd:
        return None
    if other:
        a, b = 1 / odd, 1 / other
        return a / (a + b)
    return min(0.95 / odd, 0.97)


def _single(odd):
    return min(0.95 / odd, 0.97)


def _side_of(outcome, two_way=False):
    """home / away for an outcome (None for the draw)."""
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

    # --- 1X2 -> double chance
    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))
    if h and d and a:
        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)
        ph, pd, pa = (x / total for x in inv)
        k1 = (sp.M_DC, "", sp.OUT_DC["1x"])
        k2 = (sp.M_DC, "", sp.OUT_DC["x2"])
        add("dc", f"{home} or Draw", sp.find_odds(markets, k1), ph + pd, k1, side="home")
        add("dc", f"Draw or {away}", sp.find_odds(markets, k2), pd + pa, k2, side="away")
        add("win", f"{home} to win", h, ph, (sp.M_1X2, "", sp.OUT_1X2["home"]), side="home", market_side_p=ph)
        add("win", f"{away} to win", a, pa, (sp.M_1X2, "", sp.OUT_1X2["away"]), side="away", market_side_p=pa)

    # --- goal lines
    for market in markets or []:
        if str(market.get("id")) != sp.M_TOTAL:
            continue
        spec = market.get("specifier") or ""
        if not spec.startswith("total="):
            continue
        line = _f(spec.replace("total=", ""))
        if line is None or (line * 2) % 1 != 0:
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
        add("under", f"Under {line:g} goals", under,
            _two_way(under, over), (mid, spec, sp.OUT_TOTAL["under"]), line=line)

    # --- both teams to score
    yes = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    no = sp.find_odds(markets, (sp.M_BTTS, "", sp.OUT_BTTS["no"]))
    add("btts", "Both teams to score", yes, _two_way(yes, no), (sp.M_BTTS, "", sp.OUT_BTTS["yes"]))
    add("btts", "Both teams NOT to score", no, _two_way(no, yes), (sp.M_BTTS, "", sp.OUT_BTTS["no"]))

    # --- markets found by NAME (only present when the full market list was read)
    for market in markets or []:
        mid = str(market.get("id"))
        if mid in (sp.M_1X2, sp.M_DC, sp.M_TOTAL, sp.M_BTTS):
            continue
        name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()
        spec = market.get("specifier") or ""
        outs = [o for o in market.get("outcomes", []) if o.get("isActive") is not False]

        up = re.search(r"1x2\W+([12])\s*-?\s*up\b", name)
        if up:
            n = up.group(1)
            for o in outs:
                side = _side_of(o)
                odd = _f(o.get("odds"))
                if side in ("home", "away") and odd:
                    team = home if side == "home" else away
                    add("up", f"{team} to win ({n}UP)", odd, _single(odd), (mid, spec, str(o.get("id"))),
                        side=side, market_side_p=ph if side == "home" else pa, variant=f"{n}UP")
            continue

        if "either half" in name and not any(x in name for x in ("both", "1st", "2nd")):
            for side, team in (("home", home), ("away", away)):
                for o in outs:
                    label = str(o.get("desc") or o.get("name") or "").strip().lower()
                    odd = _f(o.get("odds"))
                    if odd and (side in label or (side in name and label == "yes")):
                        add("either_half", f"{team} to win either half", odd, _single(odd),
                            (mid, spec, str(o.get("id"))), side=side,
                            market_side_p=ph if side == "home" else pa)
                        break
            continue

        if "draw no bet" in name and not any(x in name for x in ("1st", "2nd", "half")) and len(outs) == 2:
            odds = [_f(o.get("odds")) for o in outs]
            for o, odd, other in ((outs[0], odds[0], odds[1]), (outs[1], odds[1], odds[0])):
                side = _side_of(o, two_way=True)
                if side and odd:
                    team = home if side == "home" else away
                    add("dnb", f"{team} (draw no bet)", odd, _two_way(odd, other), (mid, spec, str(o.get("id"))),
                        side=side, market_side_p=ph if side == "home" else pa)
            continue

        if ("corner" in name and spec.startswith("total=")
                and not any(x in name for x in ("1st", "2nd", "first", "second", "half", "home", "away",
                                                 "team", "race", "handicap", "odd", "even", "1x2", "exact", "range"))):
            line = _f(spec.replace("total=", ""))
            if line is None or (line * 2) % 1 != 0:
                continue
            over = under = None
            over_id = under_id = None
            for o in outs:
                label = str(o.get("desc") or o.get("name") or "").strip().lower()
                if label.startswith("over") or str(o.get("id")) == sp.OUT_TOTAL["over"]:
                    over, over_id = _f(o.get("odds")), str(o.get("id"))
                elif label.startswith("under") or str(o.get("id")) == sp.OUT_TOTAL["under"]:
                    under, under_id = _f(o.get("odds")), str(o.get("id"))
            add("corners", f"Over {line:g} corners", over, _two_way(over, under), (mid, spec, over_id))
            add("corners", f"Under {line:g} corners", under, _two_way(under, over), (mid, spec, under_id))
            continue

        if ("handicap" in name and "corner" not in name and spec.startswith("hcp=") and len(outs) == 2
                and not any(x in name for x in ("1st", "2nd", "half", "3-way", "3 way", "three"))):
            try:
                line = float(spec.replace("hcp=", ""))
            except ValueError:
                continue
            odds = [_f(o.get("odds")) for o in outs]
            for o, odd, other in ((outs[0], odds[0], odds[1]), (outs[1], odds[1], odds[0])):
                side = _side_of(o, two_way=True)
                if side and odd:
                    if side == "home":
                        label = f"{home} {line:+g} handicap"
                    else:
                        label = f"{away} {-line:+g} handicap"
                    add("handicap", label, odd, _two_way(odd, other), (mid, spec, str(o.get("id"))), side=side)

    return out


def study(groups, notify=None, limit=None):
    """Read the last 5 games + head-to-head of the strongest matches (one API call each)."""
    import football_data as fd
    if fd.ENRICH_MAX <= 0 or not groups:
        return 0
    top = groups[:max(1, min(fd.ENRICH_MAX, limit or fd.ENRICH_MAX))]
    if notify:
        notify(len(top))
    studied, started = 0, time.time()
    for group in top:
        if time.time() - started > fd.ENRICH_SECONDS:
            break
        facts = fd.get_facts(fd.find_fixture(group[0]["event"]))
        if not facts:
            continue
        studied += 1
        for c in group:
            c["p"] = fd.adjusted_p(c, facts)
            c["facts"] = facts
            c["has_data"] = True
    return studied


def favourite_strength(event, side="any"):
    markets = event.get("markets") or []
    h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
    d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
    a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))
    if not (h and d and a):
        return None
    inv = [1 / h, 1 / d, 1 / a]
    total = sum(inv)
    if side == "home":
        return inv[0] / total
    if side == "away":
        return inv[2] / total
    return max(inv[0], inv[2]) / total


# ------------------------------------------------------------
# GATHER EVERYTHING SPORTYBET OFFERS IN THE WINDOW
# ------------------------------------------------------------
def gather(provider, start, end, risk, floor, exclude, min_p_shift=0.0, notify=None,
           mode="mixed", side="any", study_limit=None):
    """Returns (groups, mix_note). groups = list of lists of candidate dicts (one list per match)."""
    events = provider.get_upcoming(start, end)
    kinds = MODE_KINDS.get(mode, MODE_KINDS["mixed"])
    variant = {"up2": "2UP", "up1": "1UP"}.get(mode)
    rated = []
    for event in events:
        strength = favourite_strength(event, side)
        if strength is not None:
            rated.append((strength, event))
    if mode in ("up1", "up2"):
        # 1UP / 2UP prices of heavy favourites are below the 1.30 minimum, so look at medium favourites
        top = 0.58 if mode == "up1" else 0.68
        rated = [(strength, event) for strength, event in rated if 0.30 <= strength <= top]
    rated.sort(key=lambda x: x[0], reverse=True)

    details = {}
    needs_detail = not kinds <= {"win", "dc"}          # win / double chance are in the normal match list
    wanted = [event for _, event in rated[:MAX_DETAIL_EVENTS]] if needs_detail else []
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
            pass
        pool.shutdown(wait=False, cancel_futures=True)     # do not wait for slow ones

    min_p = MIN_LEG_PROB.get(risk, 0.68) - min_p_shift
    max_odd = MAX_LEG_ODDS.get(risk, 2.2)
    groups = []
    for event in events:
        markets = details.get(event["eventId"]) or event.get("markets") or []
        fixture = sp.sporty_fixture(event)
        kept, seen = [], set()
        for c in event_candidates(event, markets):
            if c["kind"] not in kinds or (variant and c.get("variant") != variant):
                continue
            if side != "any" and c.get("side") != side:
                continue
            if c["p"] < min_p or not (floor <= c["odd"] <= max_odd):
                continue
            if (event["eventId"], c["key"]) in exclude or c["label"] in seen:
                continue
            seen.add(c["label"])
            c.update({
                "event": event, "event_id": event["eventId"],
                "home": event.get("homeTeamName", "Home"), "away": event.get("awayTeamName", "Away"),
                "kickoff": datetime.fromtimestamp((event.get("estimateStartTime") or 0) / 1000, tz=timezone.utc),
                "league": fixture["league"]["name"],
            })
            kept.append(c)
        if kept:
            groups.append(kept)

    groups.sort(key=lambda g: max(c["p"] for c in g), reverse=True)
    groups = groups[:GROUP_LIMIT]
    studied = study(groups, notify, study_limit)
    if studied:
        refiltered = []
        for g in groups:
            g = [c for c in g if c["p"] >= min_p]
            if g:
                refiltered.append(g)
        groups = refiltered
    return groups, len(events), len(details), studied


# ------------------------------------------------------------
# PICK THE BEST COMBINATION
# ------------------------------------------------------------
def _dp(groups, target):
    """Best combination (max combined chance) whose odds land in [target, target * 1.06]."""
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
            cost = (-math.log(c["p"]) - KIND_BONUS.get(c["kind"], 0.0) + LEG_PENALTY
                    - (DATA_BONUS if c.get("has_data") else 0.0))
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
    """Run the search, then correct for rounding so the real odds land on the target (never below)."""
    aim, best = target, None
    for _ in range(5):
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
    """Keep only the best few picks of each kind so no single kind takes over."""
    drop = set()
    for kind, share in KIND_CAP.items():
        cap = max(1, math.ceil(share * legs * scale))
        ranked = sorted((c for g in groups for c in g if c["kind"] == kind), key=lambda c: c["p"], reverse=True)
        drop.update(id(c) for c in ranked[cap:])
    pruned = [[c for c in g if id(c) not in drop] for g in groups]
    return [g for g in pruned if g]


def _kind_counts(chosen):
    counts = {}
    for c in chosen:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return counts


def choose_target(groups, target, caps=True):
    """Returns (chosen, reached)."""
    all_w = sorted(math.log(c["odd"]) for g in groups for c in g)
    if not all_w:
        return [], False
    median_w = all_w[len(all_w) // 2]
    legs = max(3, round(math.log(target) / max(median_w, 0.05)))
    scale = 1.0
    for _ in range(4):
        chosen = None
        for _ in range(3):
            pruned = _prune(groups, legs, scale) if caps else groups
            chosen = _dp_exact(pruned, target) if pruned else None
            if chosen:
                break
            scale *= 1.6
        if not chosen:
            break
        counts = _kind_counts(chosen)
        if not caps or all(counts.get(k, 0) <= max(1, math.ceil(s * len(chosen) * scale)) for k, s in KIND_CAP.items()):
            return chosen, True
        legs = len(chosen)
    if chosen:
        return chosen, True
    # best effort: the most efficient pick of every match, strongest first
    best = [max(g, key=lambda c: math.log(c["odd"]) / -math.log(c["p"])) for g in groups]
    best.sort(key=lambda c: -math.log(c["p"]) / math.log(c["odd"]))
    return best[:MAX_LEGS], False


def _dp_count(groups, count, target):
    """Exactly `count` picks (one per match) whose odds land on the target, best combined chance."""
    S = 100
    tw = math.ceil(math.log(target) * S)
    wm = max(tw, int(math.log(target * (1 + OVERSHOOT)) * S)) + count
    INF = float("inf")
    dp = [[INF] * (wm + 1) for _ in range(count + 1)]
    dp[0][0] = 0.0
    back = []
    for group in groups:
        new = [row[:] for row in dp]
        step = {}
        for oi, c in enumerate(group):
            w = round(math.log(c["odd"]) * S)
            cost = -math.log(c["p"]) - (DATA_BONUS if c.get("has_data") else 0.0)
            if w <= 0 or w > wm:
                continue
            for k in range(count):
                row, nrow = dp[k], new[k + 1]
                for x in range(0, wm - w + 1):
                    base = row[x]
                    if base == INF:
                        continue
                    value = base + cost
                    if value < nrow[x + w]:
                        nrow[x + w] = value
                        step[(k + 1, x + w)] = (oi, x)
        dp = new
        back.append(step)
    best = None
    for x in range(tw, wm + 1):
        if dp[count][x] < INF and (best is None or dp[count][x] < dp[count][best]):
            best = x
    if best is None:
        return None
    picked, k, x = [], count, best
    for gi in range(len(groups) - 1, -1, -1):
        move = back[gi].get((k, x))
        if move is not None:
            picked.append(groups[gi][move[0]])
            x, k = move[1], k - 1
    picked.reverse()
    return picked if len(picked) == count else None


def choose_exact(groups, count, target):
    """`count` picks that multiply to about `target`. Returns (chosen, reached)."""
    pool = sorted(groups, key=lambda g: max(c["p"] for c in g), reverse=True)[:80]
    aim, best = target, None
    for _ in range(5):
        picks = _dp_count(pool, count, aim)
        if not picks:
            break
        actual = _product(picks)
        if actual >= target and (best is None or actual < _product(best)):
            best = picks
        if target <= actual <= target * (1 + OVERSHOOT + 0.02):
            return picks, True
        aim = max(aim * target / actual * 1.004, target)
    if best:
        return best, True
    chosen, _ = choose_count(groups, count, caps=False)
    return chosen, False


def choose_count(groups, count, caps=True):
    """The `count` strongest picks, one per match, mixing kinds."""
    ranked = sorted((c for g in groups for c in g),
                    key=lambda c: c["p"] + KIND_BONUS.get(c["kind"], 0.0) + (0.05 if c.get("has_data") else 0.0),
                    reverse=True)
    for scale in (1.0, 1.6, 3.0):
        chosen, used, counts = [], set(), {}
        for c in ranked:
            if c["event_id"] in used:
                continue
            cap = max(1, math.ceil(KIND_CAP.get(c["kind"], 0.3) * count * scale)) if caps else count
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
def _ai_json(prompt, system, max_searches=None):
    """Ask the AI (with web search when allowed). Gives up after AI_SECONDS. Returns a dict or None."""
    if not bot.ANTHROPIC_API_KEY:
        return None
    started = time.time()
    for use_tools in (True, False):
        left = AI_SECONDS - (time.time() - started)
        if left < 8:
            break
        body = {
            "model": bot.AI_MODEL,
            "max_tokens": 3000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_tools:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                              "max_uses": max_searches or WEB_SEARCHES}]
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"content-type": "application/json", "x-api-key": bot.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
        )
        try:
            with urlopen(request, timeout=left) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"AI review ({'search' if use_tools else 'plain'}) failed: {exc}")
            continue
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        parsed = bot.parse_ai_json(text)
        if parsed:
            return parsed
    return None


REVIEW_SYSTEM = """You are the analyst behind SamuelBet AI, a football ticket bot.
Each pick comes with FACTS (last-5 form, goals, head-to-head) and a DRAFT reason built from those facts.
Write the final reason for every pick: ONE or TWO plain sentences (max 30 words) that tell the reader why this match was chosen, using the facts.
Sound confident and clear, like a friend who studied the match, but stay truthful.
Rules:
- Use ONLY numbers that appear in the FACTS or in news you actually found. NEVER invent form, stats, injuries or news.
- If a pick has no facts, search the web for that match's last 5 results and head-to-head (one search per match, start with the matches that have no facts). Use only what you actually find. If you find nothing, keep the DRAFT unchanged.
- You may also search for injuries, suspensions or club news on the biggest matches.
- Set "flag" to "drop" ONLY when you found news that clearly hurts that pick (key player out, rotation, postponement). Otherwise "keep".
- No hype, no guarantees, no mention of percentages or bookmaker pricing.
Reply with ONLY this JSON, nothing else:
{"picks":[{"i":0,"reason":"...","flag":"keep"}]}"""


def ai_review(chosen, local_now):
    import football_data as fd
    lines = []
    for i, c in enumerate(chosen):
        when = c["kickoff"].astimezone(bot.LOCAL_TZ).strftime("%a %H:%M")
        facts = c.get("facts")
        lines.append(f"{i}. {c['home']} vs {c['away']} ({c['league']}) kickoff {when} | pick: {c['label']} @ {c['odd']:.2f}\n"
                     f"   FACTS: {fd.facts_digest(facts)}\n"
                     f"   DRAFT: {fd.reason_for(c, facts, c['home'], c['away'])}")
    prompt = f"Today is {local_now.strftime('%A %d %B %Y')} (Nigeria time).\n\n" + "\n".join(lines)
    data = _ai_json(prompt, REVIEW_SYSTEM, max_searches=3 if len(chosen) <= 6 else WEB_SEARCHES)
    reasons = {}
    if not data:
        return reasons
    for item in data.get("picks", []) if isinstance(data.get("picks"), list) else []:
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(chosen):
            reason = str(item.get("reason", "")).strip()[:240]
            flag = "drop" if str(item.get("flag", "")).lower() == "drop" and reason else "keep"
            if reason:
                reasons[(chosen[i]["event_id"], chosen[i]["key"])] = (reason, flag)
    return reasons


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


def build_ticket(provider, req, target, count, risk, exclude=frozenset(), notify=None, mode="mixed", side="any"):
    """Pick the matches. Extends the window by up to 2 days if the target is not reachable."""
    if target:
        floor = max(MIN_LEG_ODDS, min(1.6, target ** (1 / 26)))
    else:
        floor = MIN_LEG_ODDS
    if risk == "risky":
        floor = max(floor, 1.45)
    caps = mode == "mixed"
    # a small ticket does not need ten matches studied
    legs_guess = count or (max(3, math.ceil(math.log(target) / math.log(1.4))) if target else 5)
    study_limit = max(6, legs_guess + 3)

    def select(pool):
        if not pool:
            return [], False
        if target and count:
            return choose_exact(pool, count, target)
        if target:
            return choose_target(pool, target, caps)
        return choose_count(pool, count, caps)

    extra_days = 0
    while True:
        end = req["end"] + timedelta(days=extra_days)
        groups, total_events, detailed, studied = gather(
            provider, req["start"], end, risk, floor, exclude, 0.0, notify, mode, side, study_limit)
        # prefer the matches I studied; use the others only if those are not enough
        studied_groups = [g for g in groups if any(c.get("has_data") for c in g)]
        chosen, reached = select(studied_groups) if studied_groups else ([], False)
        if not reached:
            chosen, reached = select(groups)
        if reached or extra_days >= 2:
            break
        extra_days += 1
    note = ""
    if extra_days and chosen:
        note = f"I also used matches from the next {extra_days} day{'s' if extra_days > 1 else ''} to reach your target."
    return {"chosen": chosen, "reached": reached, "note": note,
            "events": total_events, "detailed": detailed, "floor": floor, "studied": studied}


def _launcher_send_photo(chat_id, png, caption=""):
    try:
        launcher = importlib.import_module(os.getenv("LAUNCHER_MODULE", "launcher"))
        send = getattr(launcher, "send_photo", None)
        if send:
            send(chat_id, png, caption)
    except Exception as exc:
        print(f"Ticket picture not sent: {exc}")


def detect_mode(text):
    """Ticket type and side asked for in plain words."""
    t = text.lower()
    has_home, has_away = bool(re.search(r"\bhome\b", t)), bool(re.search(r"\baway\b", t))
    side = "home" if has_home and not has_away else ("away" if has_away and not has_home else "any")
    if re.search(r"\b2\s*-?\s*up\b", t):
        mode = "up2"
    elif re.search(r"\b1\s*-?\s*up\b", t):
        mode = "up1"
    elif re.search(r"double\s*chance", t):
        mode = "dc"
    elif re.search(r"plain\s*wins?", t):
        mode = "win"
    elif re.search(r"straight\s*wins?|\b(home|away)\s*wins?\b", t):
        mode = "up2"
    else:
        mode = "mixed"
    return mode, side


def mode_title(mode, side):
    name = MODE_NAME.get(mode, "")
    if name and side != "any":
        name += f" ({side})"
    return name


def flow(chat_id, text):
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)
    if provider is None:
        return _orig_flow(chat_id, text)
    try:
        req = bot.parse_request(text)
    except Exception:
        return _orig_flow(chat_id, text)
    mode, side = detect_mode(text)
    target, count, risk = req.get("target_odds"), req.get("picks"), req.get("risk") or "normal"
    if mode != "mixed" and not target and not count:
        bot.send_message(chat_id, f"🎯 {mode_title(mode, side)} it is. How many games and how many odds do you want? "
                                  "For example: 5 games 3 odds today. You can add home or away.")
        return
    if not target and not count:
        count = 5
    return run_flow(chat_id, req, target, count, risk, mode, side, text)


def flow_spec(chat_id, spec):
    """Entry point for the website's ticket builder card."""
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)
    if provider is None:
        bot.send_message(chat_id, "SportyBet mode is off. Set USE_SPORTYBET to 1 on the server.")
        return
    req = bot.parse_request(spec.get("window") or "today")
    target, count = spec.get("odds"), spec.get("games")
    if not target and not count:
        count = 5
    return run_flow(chat_id, req, target, count, spec.get("risk") or "safe",
                    spec.get("mode") or "mixed", spec.get("side") or "any", None)


def run_flow(chat_id, req, target, count, risk, mode, side, fallback_text):
    import football_data as fd
    provider = bot.SPORTYBET_PROVIDER
    flow_started = time.time()
    fd.reset_diag()
    ask = mode_title(mode, side)
    bot.send_message(chat_id, f"⏳ Going through SportyBet's matches{' for ' + ask if ask else ''}"
                              f"{' (' + _fmt(target) + ' odds)' if target else ''}. Usually under a minute...")
    try:
        built = build_ticket(provider, req, target, count, risk, notify=lambda n: bot.send_message(
            chat_id, f"📚 Studying the last 5 games and head-to-head record of {n} matches..."),
            mode=mode, side=side)
    except Exception as exc:
        print(f"Smart ticket failed: {exc}")
        if fallback_text is not None:
            return _orig_flow(chat_id, fallback_text)
        bot.send_message(chat_id, f"❌ I could not build that ticket: {html.escape(str(exc)[:160])}")
        return

    chosen = built["chosen"]
    local_now = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ)
    if not chosen:
        extra = ""
        if mode in ("up2", "up1"):
            extra = " SportyBet may not list the 1UP/2UP market for those matches yet."
        bot.send_message(chat_id, "❌ I could not find picks on SportyBet that fit that in the window "
                                  f"(every pick must be at least {MIN_LEG_ODDS:.2f} odds).{extra} "
                                  "Try another day, side or ticket type.")
        return

    reasons, swapped = {}, []     # reasons come from the match data; the news check runs after the ticket is sent

    chosen.sort(key=lambda c: c["kickoff"])
    total_odds = 1.0
    chance = 1.0
    for c in chosen:
        total_odds *= c["odd"]
        chance *= c["p"]
        c["reason"] = reasons.get((c["event_id"], c["key"]),
                                  (fd.reason_for(c, c.get("facts"), c["home"], c["away"]), "keep"))[0]

    code, errors = None, []
    try:
        code = provider.create_booking_code([(c["event"], {"resolved_key": c["key"]}) for c in chosen])
    except Exception as exc:
        errors.append(f"Booking code failed: {exc}")

    # ---- message
    today = local_now.date()
    title = mode_title(mode, side) or (f"{_fmt(target)} ODDS" if target else f"{len(chosen)} PICKS")
    lines = [f"🎯 <b>SAMUELBET AI - {html.escape(('SAFE ' if risk == 'safe' else '') + title.upper())}</b>",
             f"📅 {html.escape(req['label'].capitalize())} • times in {bot.LOCAL_TZ_NAME}"]
    for number, c in enumerate(chosen, start=1):
        lines.append("")
        lines.append(f"<b>{number}.</b> 🕒 {html.escape(_time_text(c, today))} • 🏆 {html.escape(c['league'])}")
        lines.append(f"⚽ {html.escape(c['home'])} vs {html.escape(c['away'])}")
        lines.append(f"✅ <b>{html.escape(c['label'])}</b> • 💰 {c['odd']:.2f} • {_confidence(c['p'])} {round(c['p'] * 100)}%")
        lines.append(f"💬 {html.escape(c['reason'])}")
    lines += ["", "━━━━━━━━━━━━", f"💰 <b>Total odds: {total_odds:.2f}</b>",
              f"📊 Estimated chance of all picks winning: <b>{chance * 100:.1f}%</b>"]
    if (target and not built["reached"]) or (count and target and not built["reached"]):
        wanted = f"{count} games at {_fmt(target)} odds" if (count and target) else f"{_fmt(target)} odds"
        lines.append(f"⚠️ I could not reach {wanted} with the picks SportyBet offers right now. This is the closest I found.")
    if built["note"]:
        lines.append(f"ℹ️ {html.escape(built['note'])}")
    for c, why in swapped:
        lines.append(f"🔁 Swapped out {html.escape(c['home'])} vs {html.escape(c['away'])}: {html.escape(why)}")
    if mode == "mixed":
        mix = _kind_counts(chosen)
        lines.append("🧩 Mix: " + ", ".join(f"{n} {KIND_NAME.get(k, k).lower()}" for k, n in sorted(mix.items(), key=lambda x: -x[1])))
    if code:
        lines.append(f"📲 SportyBet code: <b>{html.escape(str(code))}</b>")
    with_data = sum(1 for c in chosen if c.get("has_data"))
    lines.append(f"📚 Last 5 games and head-to-head data was used for {with_data} of {len(chosen)} picks"
                 + ("." if with_data == len(chosen) else "; the others say so where I had none."))
    problem = fd.diag_summary()
    if problem and with_data < len(chosen):
        lines.append(f"ℹ️ Match data problem: {html.escape(problem)}.")
    lines.append(f"🔎 Read {built['events']} SportyBet matches. Every pick is at least {MIN_LEG_ODDS:.2f} odds.")
    if bot.ANTHROPIC_API_KEY and USE_AI_REVIEW:
        lines.append("📰 A quick news check follows in a moment. I cannot see line-ups, so check team news before kickoff.")
    if len(chosen) >= 12 and total_odds >= 50:
        lines.append("⚠️ Big odds win rarely, even with strong picks. Stake small.")
    lines.append("⚠️ Predictions are estimates, not guarantees (18+).")
    for error in errors:
        lines.append(f"⚠️ {html.escape(error[:160])}")
    bot.send_message(chat_id, "\n".join(lines))

    # ---- picture (picks only)
    try:
        import ticket_image_lite
        rows = [{"time": _time_text(c, today), "league": c["league"], "match": f"{c['home']} vs {c['away']}",
                 "pick": c["label"], "odd": c["odd"], "prob": c["p"]} for c in chosen]
        png = ticket_image_lite.make_ticket_image(
            rows, ("Safe " if risk == "safe" else "") + (mode_title(mode, side) or (f"{_fmt(target)} Odds" if target else f"{len(chosen)} Picks")),
            req["label"].capitalize(), total_odds, chance, code)
        _launcher_send_photo(chat_id, png, "")
    except Exception as exc:
        print(f"Ticket picture failed: {exc}")

    # the ticket is already on the user's screen: now the news check
    if USE_AI_REVIEW and bot.ANTHROPIC_API_KEY:
        news_followup(chat_id, chosen, local_now)


def news_followup(chat_id, chosen, local_now):
    """After the ticket is sent: search for news and warn about picks that look risky."""
    try:
        reasons = ai_review(chosen, local_now)
    except Exception as exc:
        print(f"News check failed: {exc}")
        return
    warn, extra = [], []
    for c in chosen:
        item = reasons.get((c["event_id"], c["key"]))
        if not item:
            continue
        text, flag = item
        name = f"{html.escape(c['home'])} vs {html.escape(c['away'])}"
        if flag == "drop":
            warn.append(f"⚠️ <b>{name}</b>: {html.escape(text)}")
        elif not c.get("has_data") and text and text != c.get("reason"):
            extra.append(f"• <b>{name}</b>: {html.escape(text)}")
    if not warn and not extra:
        bot.send_message(chat_id, "📰 News check done: nothing important found on the matches I could search.")
        return
    lines = ["📰 <b>News check</b>"]
    if warn:
        lines += warn + ["Tap Edit ticket, then Remove or Make safer on those picks."]
    if extra:
        lines += ["", "More on picks I had no match data for:"] + extra
    bot.send_message(chat_id, "\n".join(lines))


def _warm_up_loop():
    """Every few minutes, load SportyBet's matches and the full market lists of the likeliest
    matches, so a ticket request finds them ready instead of waiting. Uses no football-data calls."""
    import threading as _threading
    while True:
        try:
            provider = getattr(bot, "SPORTYBET_PROVIDER", None)
            if provider is not None:
                now = datetime.now(timezone.utc)
                events = provider.get_upcoming(now + timedelta(hours=1), now + timedelta(days=3))
                rated = [(favourite_strength(e), e) for e in events]
                rated = [(st, e) for st, e in rated if st is not None]
                strong = [e for st, e in sorted(rated, key=lambda x: x[0], reverse=True)[:50]]
                medium = [e for st, e in sorted(rated, key=lambda x: x[0], reverse=True) if 0.30 <= st <= 0.62][:50]
                seen, wanted = set(), []
                for e in strong + medium:
                    if e["eventId"] not in seen:
                        seen.add(e["eventId"])
                        wanted.append(e)
                with ThreadPoolExecutor(3) as pool:
                    list(pool.map(lambda e: _quiet(provider._event_markets_cached, e["eventId"]), wanted))
        except Exception as exc:
            print(f"Warm-up skipped: {exc}")
        time.sleep(240)


def _quiet(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


_orig_flow = bot.prediction_ticket_flow
bot.prediction_ticket_flow = flow
bot.MAX_DAYS_AHEAD = max(getattr(bot, "MAX_DAYS_AHEAD", 2), 3)

if WARM_UP:
    import threading
    threading.Thread(target=_warm_up_loop, daemon=True).start()