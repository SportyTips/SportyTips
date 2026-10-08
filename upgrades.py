"""Upgrades for SamuelBet AI. Does NOT edit main.py.

1) Straight wins use SportyBet's "1X2 - 1UP" or "1X2 - 2UP" market:
   - bot is quite sure (chance >= UP_2UP_MIN_PROB)  -> 2UP (better odds)
   - bot is less sure                               -> 1UP (safer, lower odds)
   - SportyBet does not offer it for that match     -> normal Home/Away pick
2) The AI chat talks like a real person.
3) TOP LEAGUES: when the request says "top leagues", only matches from the
   14 top competitions are used (see TOP_LEAGUE_RULES below).

Put this file next to main.py and import it in app.py AFTER the launcher.
"""

import re
import threading

import main as bot

CTX = threading.local()     # app.py sets CTX.pidgin for each chat turn

USE_UP_MARKETS = True
UP_2UP_MIN_PROB = 0.70      # lower this to use 2UP more often, raise it for more 1UP

# ------------------------------------------------------------
# 1) 1UP / 2UP
# ------------------------------------------------------------
_orig_build_options = bot.build_options


def build_options(*args, **kwargs):
    options = _orig_build_options(*args, **kwargs)
    if USE_UP_MARKETS and bot.SPORTYBET_PROVIDER is not None:
        floor = kwargs.get("min_odds")
        if floor is None:
            floor = bot.MIN_PICK_ODDS
        for option in options:
            spec = option.get("spec") or {}
            if option.get("kind") == "win" and spec.get("kind") == "win":
                spec["up"] = 2 if option["prob"] >= UP_2UP_MIN_PROB else 1
                spec["up_min_odds"] = floor
    return options


_orig_filter_available = bot.filter_available


def filter_available(*args, **kwargs):
    kept = _orig_filter_available(*args, **kwargs)
    for option in kept:
        spec = option.get("spec") or {}
        if spec.get("up_used") and not option.get("up_applied"):
            option["label"] = f"{option['label']} ({spec['up']}UP)"
            # 1UP / 2UP pay early, so they are safer than the plain win
            option["prob"] = max(option["prob"], min(0.95 / option["odd"], 0.97))
            option["up_applied"] = True
    return kept


bot.build_options = build_options
bot.filter_available = filter_available

# ------------------------------------------------------------
# 2) Human-style AI replies
# ------------------------------------------------------------
HUMAN_STYLE = """

HOW TO SOUND (this overrides the STYLE rules above and the "ONE short line" rule)
- Talk like a real friend who knows football, not like a customer-service bot.
  Use natural wording and contractions. Never start two replies the same way.
- React to what the person actually said: their mood, their team, their budget,
  their doubts. If they are joking, joke back a little. If they lost a bet, be kind.
- For "ticket" and "fixtures", "reply" is 1 to 3 natural sentences. Say what you
  are doing and why, for example why you went safe, or that big odds are a long shot.
  Do not use the same phrase every time.
- For "chat", answer like a person would in a text message: direct first, then the
  detail if it helps. Ask at most one question, and only when you really need it.
- No bullet points, no headings, no robotic phrases such as "I'd be happy to assist".
  Emojis are fine but rare (zero or one per message).
- If the person writes in pidgin or slang, you can answer in a similar easy tone.
  Otherwise use plain, warm English.
- Straight-win picks on SportyBet use the 1UP or 2UP market (the bet pays out early
  if the team goes 1 or 2 goals ahead). Explain this simply if asked.
- Stay honest: no pick is guaranteed, and you have no live scores or news.

NEW ABILITIES (these override anything above that says the bot cannot do them)
- With SportyBet on, the bot builds tickets from SportyBet's own matches. It can reach big targets
  (50 odds, 100 odds) and mixes 1UP/2UP wins, win either half, draw no bet, corners, handicap,
  both teams to score and goal lines. Never say it cannot do corners or handicap.
- It also builds straight-win tickets: "2UP win", "1UP win", "plain win" or "double chance", for home, away or both,
  for a number of games and/or total odds. For those, put the words 2up, 1up, plain win or double chance, plus
  home or away, in "request". Every pick is at least 1.30 odds.
- It studies each team's last 5 games and head-to-head record, for as many matches as the API plan allows.
- It can look at up to 3 days. A 100 odds ticket needs around 15 to 25 picks, so tell the user honestly
  that big odds win rarely even with strong picks.
- Every pick gets a short reason under it. Team news is checked when it can be found.
"""

_orig_system_prompt = bot.ai_system_prompt


PIDGIN_STYLE = """

PIDGIN MODE IS ON
Write the "reply" text in friendly Nigerian Pidgin English (for example: "No wahala, I don dey build am",
"This one strong well well"). Keep it easy to read. Keep the JSON keys and the "request" text in plain English.
"""


def ai_system_prompt():
    extra = PIDGIN_STYLE if getattr(CTX, "pidgin", False) else ""
    return _orig_system_prompt() + HUMAN_STYLE + extra


bot.ai_system_prompt = ai_system_prompt


# ------------------------------------------------------------
# 3) Smart ticket builder (big odds, mixed markets, reasons)
# ------------------------------------------------------------
try:
    import smart_ticket  # noqa: F401  (patches bot.prediction_ticket_flow)
except ImportError as exc:
    print(f"smart_ticket.py not found, using the old ticket builder: {exc}")
    smart_ticket = None


# ------------------------------------------------------------
# 4) TOP LEAGUES (14 competitions)
# ------------------------------------------------------------
# Each rule is (country word, league-name pattern).
# The country word must appear in SportyBet's country name for that match.
# An empty country word is used for the European cups.
#
# To add or remove a league, edit this list. Then update the number on the
# home card (the "14 top competitions" text is changed in app.py).

TOP_LEAGUE_RULES = [
    ("england", r"^(english )?premier league$"),
    ("italy", r"^serie a$"),
    ("spain", r"^la ?liga( ea sports)?$"),
    ("germany", r"^bundesliga$"),
    ("france", r"^ligue 1( .*)?$"),
    ("portugal", r"^(liga portugal( betclic)?|primeira liga)$"),
    ("netherlands", r"^eredivisie$"),
    ("belgium", r"^(jupiler )?pro league$|^first division a$"),
    ("turkey", r"^(trendyol )?s[uü]per lig$"),
    ("greece", r"^super ?league( 1)?$"),
    ("norway", r"^eliteserien$"),
    ("", r"^(uefa )?champions league$"),
    ("", r"^(uefa )?europa league$"),
    ("", r"^(uefa )?(europa )?conference league$"),
]

TOP_LEAGUES_RE = re.compile(r"\btop\s*-?\s*leagues?\b", re.I)

_TOP_COMPILED = [(country, re.compile(pattern)) for country, pattern in TOP_LEAGUE_RULES]

_NOT_TOP = re.compile(r"women|\(w\)|qualif|play ?offs?|youth|reserve|academy|\bu-?\d{2}\b")


def _clean(text):
    text = str(text or "").lower().replace("-", " ").replace(".", " ")
    return re.sub(r"\s+", " ", text).strip()


def is_top_league(event):
    sport = event.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}

    country = _clean(category.get("name"))
    name = _clean(tournament.get("name"))

    if not name or _NOT_TOP.search(name):
        return False

    for need, pattern in _TOP_COMPILED:
        if need and need not in country:
            continue
        if pattern.search(name):
            return True

    return False


if smart_ticket is not None:
    import sportybet_provider as sp

    _orig_get_upcoming = sp.SportyBetProvider.get_upcoming

    def get_upcoming(self, start, end):
        events = _orig_get_upcoming(self, start, end)

        if not getattr(CTX, "top_leagues", False):
            return events

        kept = [e for e in events if is_top_league(e)]
        print(f"Top leagues: kept {len(kept)} of {len(events)} matches")

        if not kept and events:
            # Help find the right names if SportyBet words them differently.
            seen = set()
            for e in events:
                cat = ((e.get("sport") or {}).get("category") or {})
                seen.add(f"{cat.get('name')} / {(cat.get('tournament') or {}).get('name')}")
                if len(seen) >= 40:
                    break
            print("Top leagues: nothing matched. Leagues seen:", sorted(seen))

        return kept

    sp.SportyBetProvider.get_upcoming = get_upcoming

    _orig_ticket_flow = bot.prediction_ticket_flow

    def prediction_ticket_flow(chat_id, text, *args, **kwargs):
        CTX.top_leagues = bool(TOP_LEAGUES_RE.search(text or ""))
        try:
            return _orig_ticket_flow(chat_id, text, *args, **kwargs)
        finally:
            CTX.top_leagues = False

    bot.prediction_ticket_flow = prediction_ticket_flow
