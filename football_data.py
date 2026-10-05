"""Real football facts for SamuelBet AI: last 5 games + head-to-head (API-Football).

One API call per match (the /predictions endpoint) returns both the team form
and the head-to-head list, so each studied match costs ONE request.

Your API plan limits how many matches can be studied per ticket:
free plan = 10 calls a minute and 100 a day. Change ENRICH_MAX (or set the
ENRICH_MAX environment variable) if you have a bigger plan.
"""

import math
import os
import re
import time
from datetime import datetime, timezone

import main as bot
import sportybet_provider as sp

ENRICH_MAX = int(os.getenv("ENRICH_MAX", "10"))           # matches studied per ticket
ENRICH_SECONDS = int(os.getenv("ENRICH_SECONDS", "45"))   # stop studying after this long
MIN_QUOTA = 6                                              # keep this many API calls spare
FACTS_CACHE_SECONDS = 3 * 3600
MIN_ODDS = 1.30          # no pick below this
SURE_P_DATA = 0.60       # "Rebuild into sure picks" keeps a match only if its best pick is this likely
SURE_P_NODATA = 0.66     # ...and this likely when I have no head-to-head data for it

_facts_cache = {}
_fixture_index = {}
_diag = {}


def reset_diag():
    _diag.clear()


def _clean(message):
    """Keep error text readable and free of words the website hides."""
    text = re.sub(r"API[- ]?Football", "the football data service", str(message), flags=re.I)
    text = text.replace("Daily API limit reached", "the daily request limit is used up")
    text = re.sub(r"plan only allows", "plan permits only", text, flags=re.I)
    text = re.sub(r"\bAPI\b", "data service", text)
    return text.replace("\n", " ")[:140]


def _note(key, detail=""):
    entry = _diag.setdefault(key, {"n": 0, "detail": ""})
    entry["n"] += 1
    if detail and not entry["detail"]:
        entry["detail"] = _clean(detail)


def diag_summary():
    """Plain-words reason why match data was missing (empty if nothing went wrong)."""
    parts = []
    if "fixtures_error" in _diag:
        parts.append(f"the football data service would not give me the match list ({_diag['fixtures_error']['detail']})")
    if "no_fixtures" in _diag:
        parts.append("the football data service has no matches listed for that day")
    if "quota" in _diag:
        parts.append("the daily request limit of the football data service is almost used up")
    if "no_match" in _diag:
        parts.append(f"{_diag['no_match']['n']} match{'es' if _diag['no_match']['n'] > 1 else ''} could not be matched to the football data service (different team names or leagues)")
    if "facts_error" in _diag:
        parts.append(f"the football data service refused the form request ({_diag['facts_error']['detail']})")
    if "no_data" in _diag:
        parts.append("the football data service has no form or head-to-head for those matches")
    return "; ".join(parts)

NO_DATA = ("I could not pull head-to-head or last-5 data for this match, so this pick rests on the "
           "bookmaker's odds, which clearly favour it.")


# ------------------------------------------------------------
# FIND THE MATCH IN API-FOOTBALL
# ------------------------------------------------------------
def _fixtures_for(date_string):
    cached = _fixture_index.get(date_string)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        fixtures = bot.get_allowed_fixtures_cached(date_string)
    except Exception as exc:
        print(f"Fixture list for {date_string} failed: {exc}")
        _note("fixtures_error", str(exc))
        fixtures = []
    _fixture_index[date_string] = (time.time(), fixtures)
    return fixtures


def find_fixture(event):
    """The API-Football fixture that matches a SportyBet event (by team names + kickoff)."""
    ms = event.get("estimateStartTime")
    if not ms:
        return None
    kickoff = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    date_string = kickoff.astimezone(bot.LOCAL_TZ).date().isoformat()
    home, away = event.get("homeTeamName", ""), event.get("awayTeamName", "")
    best, best_score = None, 0.0
    listed = _fixtures_for(date_string)
    if not listed and "fixtures_error" not in _diag:
        _note("no_fixtures")
    for fixture in listed:
        stamp = (fixture.get("fixture") or {}).get("timestamp")
        if stamp and abs(stamp - kickoff.timestamp()) > 45 * 60:
            continue
        teams = fixture.get("teams") or {}
        score = (sp._sim(home, (teams.get("home") or {}).get("name", ""))
                 + sp._sim(away, (teams.get("away") or {}).get("name", ""))) / 2
        if score > best_score:
            best, best_score = fixture, score
    if best_score < 0.72:
        if listed:
            _note("no_match")
        return None
    return best


# ------------------------------------------------------------
# FACTS
# ------------------------------------------------------------
def _num(value):
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def parse_facts(item):
    """Boil an API-Football /predictions response down to the facts we use."""
    teams = item.get("teams") or {}
    home_t, away_t = teams.get("home") or {}, teams.get("away") or {}

    def form5(team):
        return str(((team.get("league") or {}).get("form")) or "")[-5:]

    def last5_goal(team, kind):
        node = (((team.get("last_5") or {}).get("goals") or {}).get(kind) or {})
        return _num(node.get("average"))

    def venue_record(team, venue):
        fixtures = (team.get("league") or {}).get("fixtures") or {}
        get = lambda k: (fixtures.get(k) or {}).get(venue)
        return {"played": get("played"), "w": get("wins"), "d": get("draws"), "l": get("loses")}

    percent = ((item.get("predictions") or {}).get("percent")) or {}
    api = {k: (_num(percent.get(k)) or 0.0) / 100 for k in ("home", "draw", "away")}

    # head-to-head, newest first, from the home team's point of view
    home_id = home_t.get("id")
    meetings = sorted(item.get("h2h") or [], key=lambda m: str((m.get("fixture") or {}).get("date")), reverse=True)
    rows = []
    for meeting in meetings[:6]:
        goals = meeting.get("goals") or {}
        gh, ga = goals.get("home"), goals.get("away")
        if gh is None or ga is None:
            continue
        home_was_home = ((meeting.get("teams") or {}).get("home") or {}).get("id") == home_id
        rows.append((gh, ga) if home_was_home else (ga, gh))
    h2h = {"n": len(rows),
           "home_w": sum(1 for a, b in rows if a > b),
           "draws": sum(1 for a, b in rows if a == b),
           "away_w": sum(1 for a, b in rows if a < b),
           "avg_goals": (sum(a + b for a, b in rows) / len(rows)) if rows else None,
           "btts": sum(1 for a, b in rows if a > 0 and b > 0)}

    gf_h, ga_h = last5_goal(home_t, "for"), last5_goal(home_t, "against")
    gf_a, ga_a = last5_goal(away_t, "for"), last5_goal(away_t, "against")
    lam_h = (gf_h + ga_a) / 2 if None not in (gf_h, ga_a) else None
    lam_a = (gf_a + ga_h) / 2 if None not in (gf_a, ga_h) else None
    facts = {
        "form_h": form5(home_t), "form_a": form5(away_t),
        "gf_h": gf_h, "ga_h": ga_h, "gf_a": gf_a, "ga_a": ga_a,
        "lam_h": lam_h, "lam_a": lam_a,
        "rec_h": venue_record(home_t, "home"), "rec_a": venue_record(away_t, "away"),
        "h2h": h2h, "api": api,
    }
    has_something = facts["form_h"] or facts["form_a"] or h2h["n"] or gf_h is not None
    return facts if has_something else None


def get_facts(fixture):
    """Facts for one fixture (one API call, cached for 3 hours). None if unavailable."""
    if not fixture:
        return None
    fixture_id = (fixture.get("fixture") or {}).get("id")
    cached = _facts_cache.get(fixture_id)
    if cached and time.time() - cached[0] < FACTS_CACHE_SECONDS:
        return cached[1]
    remaining = getattr(bot, "_api_remaining", None)
    if remaining is not None and remaining < MIN_QUOTA:
        _note("quota")
        return None
    try:
        result = bot.football_request("predictions", {"fixture": fixture_id})
    except Exception as exc:
        print(f"Facts for fixture {fixture_id} failed: {exc}")
        _note("facts_error", str(exc))
        return None
    response = result.get("response") or []
    facts = parse_facts(response[0]) if response else None
    if facts is None:
        _note("no_data")
    _facts_cache[fixture_id] = (time.time(), facts)
    return facts


def facts_digest(facts):
    """One short line of facts for the AI reviewer."""
    if not facts:
        return "no head-to-head or last-5 data"
    h2h = facts["h2h"]
    parts = [f"last 5 form home {facts['form_h'] or '?'} / away {facts['form_a'] or '?'}"]
    if facts["gf_h"] is not None and facts["gf_a"] is not None:
        parts.append(f"last-5 goals per game home {facts['gf_h']:.1f} for {facts['ga_h']:.1f} against, "
                     f"away {facts['gf_a']:.1f} for {facts['ga_a']:.1f} against")
    if h2h["n"]:
        parts.append(f"last {h2h['n']} meetings: home won {h2h['home_w']}, draws {h2h['draws']}, away won {h2h['away_w']}"
                     + (f", {h2h['avg_goals']:.1f} goals a game" if h2h["avg_goals"] is not None else "")
                     + f", both scored {h2h['btts']}")
    rec_h, rec_a = facts["rec_h"], facts["rec_a"]
    if rec_h.get("played"):
        parts.append(f"home team won {rec_h['w']} of {rec_h['played']} at home this season")
    if rec_a.get("played"):
        parts.append(f"away team won {rec_a['w']} of {rec_a['played']} away this season")
    return "; ".join(parts)


# ------------------------------------------------------------
# PROBABILITY: nudge the market's chance with the facts
# ------------------------------------------------------------
def _poisson_cdf(k, lam):
    return sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(int(k) + 1))


def adjusted_p(c, facts):
    p = c["p"]
    if not facts:
        return p
    kind, side = c["kind"], c.get("side")
    api = facts.get("api") or {}
    if kind in ("up", "either_half", "dnb", "win") and side in ("home", "away") \
            and c.get("market_side_p") is not None and api.get(side):
        p += max(-0.08, min(0.08, 0.35 * (api[side] - c["market_side_p"])))
    lam_h, lam_a = facts.get("lam_h"), facts.get("lam_a")
    if lam_h and lam_a:
        lam = lam_h + lam_a
        if kind in ("over", "over15", "under") and c.get("line") is not None:
            under = _poisson_cdf(math.floor(c["line"]), lam)
            model = under if kind == "under" else 1 - under
            p = 0.5 * p + 0.5 * model
        elif kind == "btts":
            both = (1 - math.exp(-lam_h)) * (1 - math.exp(-lam_a))
            model = both if c["label"] == "Both teams to score" else 1 - both
            p = 0.5 * p + 0.5 * model
    return max(0.02, min(0.97, p))


# ------------------------------------------------------------
# REASONS (only real numbers from the facts)
# ------------------------------------------------------------
def _cap(text):
    return text[:1].upper() + text[1:] if text else text


def _win_like(c, facts, home, away):
    side = c.get("side")
    if side not in ("home", "away"):
        return None
    team, opp = (home, away) if side == "home" else (away, home)
    form = facts["form_h"] if side == "home" else facts["form_a"]
    opp_form = facts["form_a"] if side == "home" else facts["form_h"]
    rec = facts["rec_h"] if side == "home" else facts["rec_a"]
    h2h = facts["h2h"]
    first = []
    if form:
        first.append(f"{team} won {form.count('W')} of their last {len(form)} games ({form})")
    if rec and rec.get("played"):
        first.append(f"{'at home' if side == 'home' else 'away'} they have won {rec['w']} of {rec['played']} this season")
    sentence = _cap(" and ".join(first) + ".") if first else ""
    second = ""
    if h2h["n"] >= 3:
        mine = h2h["home_w"] if side == "home" else h2h["away_w"]
        second = f" In the last {h2h['n']} meetings with {opp} they won {mine}."
    elif opp_form:
        second = f" {opp} won only {opp_form.count('W')} of their last {len(opp_form)} ({opp_form})."
    kind = c["kind"]
    tail = ""
    if kind == "up":
        n = "2" if "2UP" in c["label"] else "1"
        tail = f" The {n}UP market pays out as soon as they lead by {n}."
    elif kind == "either_half":
        tail = " They only need to win one half."
    elif kind == "dnb":
        tail = " A draw returns your stake."
    return (sentence + second + tail).strip() or None


def _dc_like(c, facts, home, away):
    side = c.get("side")
    if side not in ("home", "away"):
        return None
    team = home if side == "home" else away
    form = facts["form_h"] if side == "home" else facts["form_a"]
    if not form:
        return None
    text = f"{team} lost only {form.count('L')} of their last {len(form)} games ({form})."
    h2h = facts["h2h"]
    if h2h["n"] >= 3:
        lost = h2h["away_w"] if side == "home" else h2h["home_w"]
        text += f" They lost {lost} of the last {h2h['n']} meetings."
    return text


def _goals_like(c, facts, home, away):
    kind = c["kind"]
    gf_h, ga_h, gf_a, ga_a = facts["gf_h"], facts["ga_h"], facts["gf_a"], facts["ga_a"]
    h2h = facts["h2h"]
    avg = f" The last {h2h['n']} meetings averaged {h2h['avg_goals']:.1f} goals." if h2h["n"] >= 3 and h2h["avg_goals"] is not None else ""
    if None in (gf_h, ga_h, gf_a, ga_a):
        return None
    if kind in ("over", "over15"):
        return f"{home} score {gf_h:.1f} and {away} {gf_a:.1f} goals a game over their last 5.{avg}"
    if kind == "under":
        return f"{home} concede {ga_h:.1f} and {away} {ga_a:.1f} a game over their last 5, so goals should be limited.{avg}"
    if kind == "btts":
        both = f" Both teams scored in {h2h['btts']} of the last {h2h['n']} meetings." if h2h["n"] >= 3 else ""
        if c["label"] == "Both teams to score":
            return f"{home} score {gf_h:.1f} and {away} {gf_a:.1f} a game over their last 5.{both}"
        return f"{home} concede {ga_h:.1f} and {away} {ga_a:.1f} a game over their last 5, so one side may stay quiet.{both}"
    if kind == "corners":
        return (f"{home} score {gf_h:.1f} and {away} {gf_a:.1f} a game over their last 5, "
                "the kind of attacking games that usually bring plenty of corners.")
    return None


def reason_for(c, facts, home, away):
    """A short, honest reason for a pick, built only from real numbers."""
    if not facts:
        return NO_DATA
    kind = c["kind"]
    text = None
    if kind in ("up", "either_half", "dnb", "win", "handicap"):
        text = _win_like(c, facts, home, away)
        if text and kind == "handicap":
            text += " That makes the handicap line look comfortable."
    elif kind == "dc":
        text = _dc_like(c, facts, home, away)
    else:
        text = _goals_like(c, facts, home, away)
    return text or NO_DATA


# ------------------------------------------------------------
# FOR TICKETS THE USER PASTES
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
    """Turn a leg of a pasted code into the same kind/side/line the builder uses."""
    mid, spec, oid = leg["key"]
    mname = str(leg.get("market_name") or "").lower()
    oname = str(leg.get("outcome_name") or "").lower()
    out = {"label": sp.leg_label(leg), "side": None, "line": None}
    if mid == sp.M_1X2:
        out["kind"], out["side"] = "win", {"1": "home", "3": "away"}.get(oid)
    elif mid == sp.M_DC:
        out["kind"], out["side"] = "dc", {"9": "home", "11": "away"}.get(oid)
    elif mid == sp.M_TOTAL:
        out["line"] = sp._float(spec.replace("total=", ""))
        out["kind"] = "over" if oid == sp.OUT_TOTAL["over"] else "under"
    elif mid == sp.M_BTTS:
        out["kind"] = "btts"
    elif "either half" in mname:
        out["kind"], out["side"] = "either_half", ("away" if "away" in oname else "home")
    elif "1x2" in mname and "up" in mname:
        out["kind"], out["side"] = "up", ("away" if (oname.startswith("away") or oid == "3") else "home")
    elif "corner" in mname:
        out["kind"] = "corners"
    elif "handicap" in mname:
        out["kind"], out["side"] = "handicap", ("away" if (oname.startswith("away") or oid == "2") else "home")
    elif "draw no bet" in mname:
        out["kind"], out["side"] = "dnb", ("away" if (oname.startswith("away") or oid == "5") else "home")
    else:
        out["kind"] = "other"
    return out


def why(provider, code, index):
    """The reason for one pick of a pasted code."""
    reset_diag()
    legs = provider.load_code(code)
    if not (0 <= index < len(legs)):
        raise sp.SportyBetError("That pick is no longer on the ticket.")
    leg = legs[index]
    event = event_for(provider, leg["event_id"])
    facts = None
    if event is not None:
        facts = get_facts(find_fixture(event))
    c = classify_leg(leg)
    text = reason_for(c, facts, leg["home"], leg["away"])
    if not facts and diag_summary():
        text += f" (Why: {diag_summary()}.)"
    return {"reason": text, "has_data": bool(facts)}


def _rank(provider, event, use_data):
    """Every pick this match offers, strongest first, plus the facts used."""
    import smart_ticket
    try:
        markets = provider._event_markets_cached(event["eventId"])
    except Exception:
        markets = None
    markets = markets or event.get("markets") or []
    facts = get_facts(find_fixture(event)) if use_data else None
    ranked = []
    allowed = smart_ticket.MODE_KINDS["mixed"]
    for c in smart_ticket.event_candidates(event, markets):
        if c["kind"] not in allowed:
            continue
        c["p"] = adjusted_p(c, facts)
        c["has_data"] = bool(facts)
        if c["odd"] >= MIN_ODDS:
            ranked.append(c)
    ranked.sort(key=lambda c: c["p"] + 0.1 * min(c["odd"] - 1, 0.4), reverse=True)
    return ranked, facts


def safest_for_leg(provider, leg):
    """(key, odd, label, reason) for a safer pick on this match, or None."""
    event = event_for(provider, leg["event_id"])
    if event is None:
        return None
    ranked, facts = _rank(provider, event, True)
    if not ranked:
        return None
    best = ranked[0]
    current = min(0.95 / (leg.get("odd") or 1.0), 0.97) if (leg.get("odd") or 0) > 1 else 0.97
    if best["key"] == leg["key"] or best["p"] <= current:
        return None
    return best["key"], best["odd"], best["label"], reason_for(best, facts, leg["home"], leg["away"])


def rebuild_safer(provider, code):
    """Turn a whole code into a sure ticket: for every match pick the most likely
    pick using form + head-to-head, and drop matches with no sure pick."""
    reset_diag()
    legs = provider.load_code(code)
    started = time.time()
    studied, new_legs, dropped = 0, [], []
    for leg in legs:
        event = event_for(provider, leg["event_id"])
        match = f"{leg['home']} vs {leg['away']}"
        if event is None:
            dropped.append({"match": match, "why": "this match is no longer on SportyBet's list"})
            continue
        use_data = studied < ENRICH_MAX and time.time() - started < ENRICH_SECONDS
        ranked, facts = _rank(provider, event, use_data)
        if use_data:
            studied += 1
        best = ranked[0] if ranked else None
        needed = SURE_P_DATA if facts else SURE_P_NODATA
        if best is None or best["p"] < needed:
            dropped.append({"match": match, "why": "no pick I would call sure"})
            continue
        new_legs.append({
            "event_id": event["eventId"], "home": leg["home"], "away": leg["away"],
            "key": best["key"], "odd": best["odd"], "label_override": best["label"],
            "reason": reason_for(best, facts, leg["home"], leg["away"]),
        })
    if not new_legs:
        raise sp.SportyBetError("None of those matches had a pick I would call sure. Try a different code.")
    new_code = provider._save_code([(l["event_id"], l["key"]) for l in new_legs])
    result = sp.summarize_legs(new_legs)
    result.update({"code": new_code, "changed": True, "dropped": dropped, "studied": studied,
                   "data_note": diag_summary()})
    return result