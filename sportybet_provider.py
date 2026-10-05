"""SportyBet provider for SportyTips.

Needs no extra libraries. Turn on with:  USE_SPORTYBET=1

SportyBet has NO official public API. Everything marked VERIFY below is based
on the web app's requests and must be checked in your browser (F12 -> Network)
because SportyBet can change it at any time.
"""

import json
import re
import time
from difflib import SequenceMatcher
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.sportybet.com/api/ng"
UPCOMING_PATH = "/factsCenter/pcUpcomingEvents"
EVENT_PATH = "/factsCenter/event"
LOAD_CODE_PATH = "/orders/share/{code}"
SAVE_CODE_PATH = "/orders/share"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "clientid": "web",
    "operid": "2",
    "platform": "web",
}

TIMEOUT = 20
EVENTS_CACHE_SECONDS = 300
MAX_EVENT_PAGES = 15
MIN_SAFER_ODDS = 1.05

M_1X2 = "1"
M_DC = "10"
M_TOTAL = "18"
M_BTTS = "29"
M_HOME_TEAM_GOALS = "19"
M_AWAY_TEAM_GOALS = "20"
M_CORNERS = "166"
M_CORNERS_1H = "177"
M_STREAK_3 = "60020"
M_DNB = "64"
M_HANDICAP = "65"
M_ASIAN_HANDICAP = "66"
M_GOALS_BY_MINUTE = "900313"

OUT_1X2 = {"home": "1", "draw": "2", "away": "3"}
OUT_DC = {"1x": "9", "12": "10", "x2": "11"}
OUT_TOTAL = {"over": "12", "under": "13"}
OUT_BTTS = {"yes": "74", "no": "76"}
OUT_STREAK = {"yes": "74", "no": "76"}
OUT_TEAM_GOALS = {"over": "12", "under": "13"}
OUT_DNB = {"home": "4", "away": "5"}

PREFER_EITHER_HALF = True
MIN_UP_ODDS_DEFAULT = 1.       20
UP_MARKETS_CACHE_SECONDS = 600

VIRTUAL_RE = re.compile(
    r"\bsrl\b|simulated|virtual|e-?soccer|e-?football|esports?|cyber|volta|"
    r"battle\s*-?\s*\d+\s*min",
    re.I,
)


class SportyBetError(Exception):
    pass


def _norm(name):
    name = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    name = re.sub(r"\b(fc|cf|sc|afc|ac|as|fk|sk|club|de|the)\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _sim(a, b):
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _walk_events(node):
    if isinstance(node, dict):
        if "eventId" in node and "homeTeamName" in node:
            yield node
            return
        for value in node.values():
            yield from _walk_events(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_events(value)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_virtual(event):
    sport = event.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    text = " ".join(str(part or "") for part in (
        event.get("homeTeamName"), event.get("awayTeamName"),
        tournament.get("name"), category.get("name"),
    ))
    return bool(VIRTUAL_RE.search(text))


def find_up_market(markets, spec):
    n = spec.get("up")
    side = spec.get("side")
    if n not in (1, 2) or side not in ("home", "away"):
        return None
    pattern = re.compile(r"1x2\W+" + str(n) + r"\s*-?\s*up\b", re.I)
    for market in markets or []:
        text = f"{market.get('desc') or ''} {market.get('name') or ''}"
        if not pattern.search(text):
            continue
        specifier = market.get("specifier") or ""
        for outcome in market.get("outcomes", []):
            label = str(outcome.get("desc") or outcome.get("name") or "").strip().lower()
            if not (label == side or str(outcome.get("id")) == OUT_1X2[side]):
                continue
            if outcome.get("isActive") is False:
                return None
            odd = _float(outcome.get("odds"))
            if odd:
                return (str(market.get("id")), specifier, str(outcome.get("id"))), odd
    return None


def find_either_half(markets, side):
    for market in markets or []:
        text = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()
        if "either half" not in text or "both" in text or "1st" in text or "2nd" in text:
            continue
 specifier = market.get("specifier") or ""
        for outcome in market.get("outcomes", []):
            label = str(outcome.get("desc") or outcome.get("name") or "").strip().lower()
            if not (side in label or (side in text and label == "yes")):
                continue
            if outcome.get("isActive") is False:
                return None
            odd = _float(outcome.get("odds"))
            if odd:
                return (str(market.get("id")), specifier, str(outcome.get("id"))), odd
    return None


def spec_to_key(spec):
    if spec.get("resolved_key"):
        return spec["resolved_key"]
    kind, side = spec.get("kind"), spec.get("side")
    if kind == "dc":
        return M_DC, "", OUT_DC[side]
    if kind == "goals":
        return M_TOTAL, "total=" + format(spec["line"], "g"), OUT_TOTAL[side]
    if kind == "btts":
        return M_BTTS, "", OUT_BTTS[side]
    if kind == "team_goals":
        mid = M_HOME_TEAM_GOALS if side == "home" else M_AWAY_TEAM_GOALS
        return mid, "total=" + format(spec["line"], "g"), OUT_TEAM_GOALS["over"]
    if kind == "streak":
        return M_STREAK_3, "", OUT_STREAK[spec.get("side", "no")]
    if kind == "corners":
        mid = M_CORNERS_1H if spec.get("half") else M_CORNERS
        return mid, "total=" + format(spec["line"], "g"), OUT_TOTAL["over"]
    if kind == "dnb":
        return M_DNB, "", OUT_DNB[side]
    return None


def key_to_label(key, home, away):
    market, specifier, outcome = key
    if market == M_DC:
        return {"9": home + " or Draw", "10": home + " or " + away,
                "11": "Draw or " + away}.get(outcome, "Double chance")
    if market == M_TOTAL:
        line = specifier.replace("total=", "")
        return ("Over " if outcome == OUT_TOTAL["over"] else "Under ") + line + " goals"
    if market == M_BTTS:
        return "Both teams to score" if outcome == OUT_BTTS["yes"] else "Both teams NOT to score"
    if market in (M_HOME_TEAM_GOALS, M_AWAY_TEAM_GOALS):
        line = specifier.replace("total=", "")
        team = home if market == M_HOME_TEAM_GOALS else away
        return team + " to score " + line + "+"
    if market == M_STREAK_3:
        return "No team to score 3+ in a row" if outcome == OUT_STREAK["no"] else "Any team to score 3+ in a row"
    if market == M_CORNERS:
        line = specifier.replace("total=", "")
        return "Over " + line + " corners"
    if market == M_CORNERS_1H:
        line = specifier.replace("total=", "")
        return "1st half Over " + line + " corners"
    if market == M_DNB:
        team = home if outcome == OUT_DNB["home"] else away
        return team + " (draw no bet)"
    return "Market " + market + " (" + specifier + ") outcome " + outcome


def find_odds(markets, key):
    market_id, specifier, outcome_id = key
    for market in markets or []:
        if str(market.get("id")) != market_id:
            continue
        if (market.get("specifier") or "") != specifier:
            continue
        for outcome in market.get("outcomes", []):
            if str(outcome.get("id")) == outcome_id:
                if outcome.get("isActive") is False:
                    return None
                return _float(outcome.get("odds"))
    return None


def safer_alternatives(key):
    market, specifier, outcome = key
    if market == M_DC:
        return []
    if market == M_TOTAL:
        line = _float(specifier.replace("total=", "")) or 0
        if outcome == OUT_TOTAL["over"] and line > 1.5:
            return [(M_TOTAL, "total=1.5", OUT_TOTAL["over"])]
        if outcome == OUT_TOTAL["under"] and line < 3.5:
            return [(M_TOTAL, "total=3.5", OUT_TOTAL["under"])]
        return []
    if market == M_BTTS:
        if outcome == OUT_BTTS["yes"]:
            return [(M_TOTAL, "total=1.5", OUT_TOTAL["over"])]
        return [(M_TOTAL, "total=3.5", OUT_TOTAL["under"])]
    return []


def sporty_fixture(event):
    sport = event.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    return {
        "fixture": {
            "id": event.get("eventId"),
            "timestamp": int((event.get("estimateStartTime") or 0) / 1000),
            "status": {"short": "NS"},
        },
        "teams": {
            "home": {"name": event.get("homeTeamName", "Home")},
            "away": {"name": event.get("awayTeamName", "Away")},
        },
        "league": {
            "id": None,
            "name": tournament.get("name") or "Football",
            "country": category.get("name") or "",
        },
    }


def can_safer(key):
    straight = key[0] in (M_DNB,)
    return bool(safer_alternatives(key)) or straight


def leg_label(leg):
    if leg.get("label_override"):
        return leg["label_override"]
    key = leg["key"]
    return key_to_label(key, leg["home"], leg["away"])


def leg_risk(leg):
    odd = leg.get("odd") or 1.0
    score = 0 if odd < 1.35 else (1 if odd < 1.6 else 2)
    if leg["key"][0] in (M_BTTS, M_HOME_TEAM_GOALS, M_AWAY_TEAM_GOALS, M_STREAK_3):
        score += 1
    return "Safe" if score == 0 else ("Medium" if score == 1 else "Risky")


def summarize_legs(legs):
    rows, total, chance, straight = [], 1.0, 1.0, 0
    for index, leg in enumerate(legs):
        odd = leg.get("odd") or 1.0
        total *= odd
        chance *= min(0.95 / odd, 0.97) if odd > 1 else 0.97
        if leg["key"][0] in (M_DNB,):
            straight += 1
        rows.append({
            "index": index,
            "match": leg["home"] + " vs " + leg["away"],
            "pick": leg_label(leg),
            "odd": odd,
            "risk": leg_risk(leg),
            "can_safer": can_safer(leg["key"]),
            "reason": leg.get("reason"),
        })
    health = "Safe" if chance >= 0.45 else ("Medium" if chance >= 0.20 else "Risky")
    reason = str(len(rows)) + " pick" + ("s" if len(rows) != 1 else "") + ", total odds " + format(total, ".2f") + "."
    if straight:
        reason += " " + str(straight) + " straight win" + ("s" if straight != 1 else "") + "."
    return {"rows": rows, "total_odds": total, "chance": round(chance * 100),
            "health": health, "reason": reason, "straight": straight}


class SportyBetProvider:
    def __init__(self):
        self._events = []
        self._events_time = 0
        self._up_cache = {}

    def get_upcoming(self, start, end):
        found = []
        for event in self._load_events():
            ms = event.get("estimateStartTime")
            if ms and start.timestamp() <= ms / 1000 < end.timestamp():
                found.append(event)
        return found

    def _request(self, path, params=None, body=None):
        url = BASE + path
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=data, headers=HEADERS,
                          method="POST" if body is not None else "GET")
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise SportyBetError("HTTP " + str(exc.code) + " on " + path)
        except (URLError, ValueError) as exc:
            raise SportyBetError(path + ": " + str(exc))
        if result.get("bizCode") not in (None, 10000):
            raise SportyBetError(path + ": " + str(result.get("message") or result.get("bizCode")))
        return result

    def _load_events(self):
        if self._events and time.time() - self._events_time < EVENTS_CACHE_SECONDS:
            return self._events
        events = []
        for page in range(1, MAX_EVENT_PAGES + 1):
            result = self._request(UPCOMING_PATH, {
                "sportId": "sr:sport:1",
                "marketId": M_1X2 + "," + M_DC + "," + M_TOTAL + "," + M_BTTS,
                "pageSize": 100,
                "pageNum": page,
                "todayGames": "false",
            })
            found = list(_walk_events(result.get("data")))
            if not found:
                break
            events.extend(found)
        events = [event for event in events if not is_virtual(event)]
        self._events, self._events_time = events, time.time()
        return events

    def find_event(self, home, away, kickoff, league_name):
        best, best_score = None, 0.0
        for event in self._load_events():
            start_ms = event.get("estimateStartTime")
            if kickoff and start_ms:
                if abs(start_ms / 1000 - kickoff.timestamp()) > 20 * 60:
                    continue
            score = (_sim(home, event.get("homeTeamName", ""))
                     + _sim(away, event.get("awayTeamName", ""))) / 2
            if score > best_score:
                best, best_score = event, score
        return best if best_score >= 0.70 else None

    def _event_markets_cached(self, event_id):
        hit = self._up_cache.get(event_id)
        if hit and time.time() - hit[0] < UP_MARKETS_CACHE_SECONDS:
            return hit[1]
        markets = self.get_event_markets(event_id)
        self._up_cache[event_id] = (time.time(), markets)
        return markets

    def get_odds(self, event, spec):
        if spec.get("up"):
            try:
                found = find_up_market(self._event_markets_cached(event["eventId"]), spec)
            except SportyBetError:
                found = None
            floor = spec.get("up_min_odds") or MIN_UP_ODDS_DEFAULT
            if found and found[1] >= floor:
                spec["resolved_key"] = found[0]
                spec["up_used"] = True
                return found[1]
        spec.pop("resolved_key", None)
        spec["up_used"] = False
        key = spec_to_key(spec)
        if key is None:
            return None
        return find_odds(event.get("markets"), key)

    def create_booking_code(self, selections):
        keys = []
        for event, spec in selections:
            key = spec_to_key(spec)
            if key is None:
                raise SportyBetError("Unsupported market for booking: " + str(spec.get("kind")))
            keys.append((event["eventId"], key))
        return self._save_code(keys)

    def _save_code(self, keys):
        body = {"selections": [
            {"eventId": event_id, "marketId": k[0], "specifier": k[1], "outcomeId": k[2]}
            for event_id, k in keys
        ]}
        result = self._request(SAVE_CODE_PATH, body=body)
        data = result.get("data") or {}
        code = data.get("shareCode") or data.get("code")
        if not code:
            raise SportyBetError("SportyBet did not return a code.")
        return code

    def load_code(self, code):
        result = self._request(LOAD_CODE_PATH.format(code=code))
        data = result.get("data") or {}
        legs = []
        for event in data.get("outcomes", []):
            for market in event.get("markets", []):
                chosen = [o for o in market.get("outcomes", [])
                          if o.get("isSelected") or o.get("selected")]
                if not chosen and len(market.get("outcomes", [])) == 1:
                    chosen = market["outcomes"]
                for outcome in chosen:
                    legs.append({
                        "event_id": event.get("eventId"),
                        "home": event.get("homeTeamName", "Home"),
                        "away": event.get("awayTeamName", "Away"),
                        "key": (str(market.get("id")), market.get("specifier") or "",
                                str(outcome.get("id"))),
                        "odd": _float(outcome.get("odds")),
                        "market_name": market.get("desc") or market.get("name") or "",
                        "outcome_name": outcome.get("desc") or outcome.get("name") or "",
                    })
        if not legs:
            raise SportyBetError("Could not read that code (expired, or the format changed).")
        return legs

    def get_event_markets(self, event_id):
        result = self._request(EVENT_PATH, {"eventId": event_id, "productId": 3})
        return (result.get("data") or {}).get("markets", [])

    def check_code(self, code):
        legs = self.load_code(code)
        result = summarize_legs(legs)
        result["code"] = code
        return result

    def _safer_pick(self, leg):
        key = leg["key"]
        is_win = key[0] == M_DNB
        alternatives = safer_alternatives(key)
        if not (alternatives or is_win):
            return None
        try:
            markets = self.get_event_markets(leg["event_id"])
        except SportyBetError:
            return None
        options = []
        for alt in alternatives:
            odd = find_odds(markets, alt)
            if odd and odd >= MIN_SAFER_ODDS:
                options.append((odd, alt))
        if options:
            odd, alt = min(options)
            return alt, odd, key_to_label(alt, leg["home"], leg["away"])
        return None

    def edit_code(self, code, remove=None, swap=None, picker=None):
        legs = self.load_code(code)
        remove, swap = set(remove or []), set(swap or [])
        new_legs, changed = [], False
        for index, leg in enumerate(legs):
            if index in remove:
                changed = True
                continue
            leg = dict(leg)
            if index in swap:
                better = (picker(leg) if picker else None) or self._safer_pick(leg)
                if better:
                    leg["key"], leg["odd"], leg["label_override"] = better[:3]
                    leg["reason"] = better[3] if len(better) > 3 else None
                    changed = True
            new_legs.append(leg)
        if not new_legs:
            raise SportyBetError("A ticket needs at least one pick.")
        new_code = code
        if changed:
            new_code = self._save_code([(l["event_id"], l["key"]) for l in new_legs])
        result = summarize_legs(new_legs)
        result["code"] = new_code
        result["changed"] = changed
        return result

    def make_safer(self, code):
        legs = self.load_code(code)
        rows, new_keys, seen = [], [], set()
        old_total = new_total = 1.0
        for leg in legs:
            old_key, old_odd = leg["key"], leg["odd"] or 1.0
            new_key, new_odd = old_key, old_odd
            changed = False
            alternatives = safer_alternatives(old_key)
            is_win = old_key[0] == M_DNB
            if alternatives or is_win:
                try:
                    markets = self.get_event_markets(leg["event_id"])
                except SportyBetError:
                    markets = []
                options = []
                for alt in alternatives:
                    odd = find_odds(markets, alt)
                    if odd and odd >= MIN_SAFER_ODDS:
                        options.append((odd, alt))
                if options:
                    new_odd, new_key = min(options)
                    changed = True
            old_total *= old_odd
            duplicate = (leg["event_id"], new_key) in seen
            if duplicate:
                rows.append({
                    "match": leg["home"] + " vs " + leg["away"],
                    "old": key_to_label(old_key, leg["home"], leg["away"]),
                    "old_odd": old_odd,
                    "new": "removed (already covered by another pick)",
                    "new_odd": 1.0,
                    "changed": True,
                })
                continue
            seen.add((leg["event_id"], new_key))
            new_total *= new_odd
            new_keys.append((leg["event_id"], new_key))
            rows.append({
                "match": leg["home"] + " vs " + leg["away"],
                "old": key_to_label(old_key, leg["home"], leg["away"]),
                "old_odd": old_odd,
                "new": key_to_label(new_key, leg["home"], leg["away"]),
                "new_odd": new_odd,
                "changed": changed,
            })
        new_code = self._save_code(new_keys) if any(r["changed"] for r in rows) else None
        return {"rows": rows, "old_total": old_total,
                "new_total": new_total, "new_code": new_code}
