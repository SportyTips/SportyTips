"""Upgrades for SamuelBet AI. Does NOT edit main.py.

1) Straight wins use SportyBet's "1X2 - 1UP" or "1X2 - 2UP" market:
- bot is quite sure (chance >= UP_2UP_MIN_PROB) -> 2UP (better odds)
- bot is less sure -> 1UP (safer, lower odds)
- SportyBet does not offer it for that match -> normal Home/Away pick
2) The AI chat talks like a real person.
3) TOP LEAGUES: the card opens a window that asks for the odds, then builds a
predicted ticket from the 14 top leagues. "Top leagues games this week" lists every
game instead. The Champions / Europa / Conference League are NOT in Top leagues: they
have their own card ("European cups") where each cup can be chosen alone.
4) STRAIGHT WIN (button and chat): ALL leagues. Only the Top leagues card is limited to the top leagues. Strong teams (55%+ chance to win): 1UP price
1.20 to 1.32, 2UP price 1.30 to 1.50, never higher. Soonest games first, up to a week.
Everywhere: no direct win (only 1UP / 2UP), no BTTS, no Over 2 / 2.5 goals.
Straight win is ONLY 1UP / 2UP (plus win either half when asked): no "Home or Away" (12) picks.
If the odds cannot be reached, the ticket simply stops at the picks it found.
4a) CHAT: "send today's straight win 30 odds" -> today's games first (until 8pm); if the odds
cannot be reached, the next day is added, then the next, up to SEARCH_DAYS. Same for "tomorrow". "send today's straight win and win either half"
adds "win either half" picks next to the 1UP / 2UP picks.
5) "weekend" now means Friday to Sunday.
5a) ALL TICKETS use ONLY these picks - 1UP, 2UP and win either half (ONLY_UP_AND_EITHER_HALF = True).
No goal bounds, no Home or Away (12), no team goals, no corners, no over/under goals.
5b) NO CORNERS anywhere (NO_CORNERS = True) and NO "3 goals in a row" picks (NO_STREAK = True).
Normal tickets also include individual team goals (Home / Away to score 0.5 or 1.5).
6) Under every ticket the site says whether real football stats were used.
7) TOP LEAGUES / CUPS are now built "safe" (same as when you ask in the chat) and need
STRONG_MIN_P_TOP. DAILY 2 ODDS needs DAILY_MIN_P (strong picks only).
8) "tonight" / "night games" = 9pm to 5am. "midnight" = 10pm to 5am. Never borrows tomorrow's games.
9) "today" (no time word) = games from now until 8pm only (TODAY_END_HOUR).
10) Chat: "are there any midnight games?" -> yes/no + kickoff times, then "list" shows the games and
"yes" / "yeah" / "build it" builds a safe ticket from them. "show tomorrow morning games" lists them.

Put this file next to main.py and import it in app.py AFTER the launcher.
"""

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape

import main as bot

CTX = threading.local()  # app.py sets CTX.pidgin for each chat turn

USE_UP_MARKETS = True
UP_2UP_MIN_PROB = 0.70  # lower this to use 2UP more often, raise it for more 1UP

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
  (50 odds, 100 odds) and uses ONLY these picks: 1UP, 2UP and win either half. It does NOT use goal bounds,
  corners, Home or Away (12), team goals, over/under goals, BTTS, handicap, direct wins or "3 goals in a row":
  say so if asked.
- It also builds straight-win tickets: "2UP win", "1UP win", "plain win" or "double chance", for home, away or both,
  for a number of games and/or total odds. For those, put the words 2up, 1up, plain win or double chance, plus
  home or away, in "request". Every pick is at least 1.30 odds.
- "Today's straight win 30 odds" uses today's games first and adds tomorrow's only if 30 odds cannot be reached.
  "Straight win and win either half" mixes 1UP / 2UP picks with "win either half" picks. Keep the words
  "straight win", "today", the odds number and "either half" EXACTLY in "request".
- It studies each team's last 5 games and head-to-head record, for as many matches as the API plan allows.
- It can look at up to 7 days. A 100 odds ticket needs around 15 to 25 picks, so tell the user honestly
  that big odds win rarely even with strong picks.
- Every pick gets a short reason under it. Team news is checked when it can be found.
- TIME WORDS it understands: morning (5am-12pm), afternoon (12pm-5pm), evening (5pm-9pm), tonight or night
  (9pm-5am), midnight (10pm-5am), today (now until 8pm) and tomorrow. Keep these words EXACTLY in "request",
  for example "safe 5 odds tomorrow morning". "tomorrow" alone means the whole day: never turn it into
  morning or night.
- It can use U20 / U21 / youth games and women's leagues too. Never say it cannot.
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
    import smart_ticket  # noqa: F401 (patches bot.prediction_ticket_flow)
except ImportError as exc:
    print(f"smart_ticket.py not found, using the old ticket builder: {exc}")
    smart_ticket = None


# ------------------------------------------------------------
# SETTINGS YOU CAN CHANGE
# ------------------------------------------------------------
ONLY_UP_AND_EITHER_HALF = True  # True = EVERY ticket uses ONLY 1UP, 2UP and win either half (no goal bounds, 12, team goals, corners ...)
NO_CORNERS = True  # True = corners (total and each team) are never used anywhere
NO_STREAK = True  # True = "Any team to score 3+ goals in a row" is never used

SEARCH_DAYS = 7  # how far ahead the bot may look (days)
EVENT_PAGES = 60  # how many pages of SportyBet matches to read (100 matches per page)

# Memory safety (Render free plan has 512 MB)
MAX_DETAIL = 70  # how many matches get their full list of markets read
DETAIL_WORKERS = 6  # how many are read at the same time
CACHE_LIMIT = 160  # how many matches are remembered in memory

# Straight win button (1UP / 2UP, strong teams from the top leagues)
STRAIGHT_LEGS = 10  # how many games to aim for
STRAIGHT_MAX_LEGS = 30  # most picks a straight win ticket may have (30 odds needs about 12 to 15)
STRAIGHT_MIN_P = 0.30  # the pick itself must have at least this chance after the checks
STRAIGHT_DETAIL = 150  # for Straight win: how many matches get their 1UP / 2UP prices read
STRAIGHT_STRONG_P = 0.55  # a team needs at least this chance to win (from SportyBet's prices)
STRAIGHT_MAX_STRENGTH = 0.88  # teams stronger than this pay too little, so they are not read
STRAIGHT_2UP_FROM_P = 0.60  # 55% to 59% -> 1UP. 60% and above -> 2UP (when its price fits)
STRAIGHT_1UP_MIN = 1.20  # 1UP pays at least this ...
STRAIGHT_1UP_MAX = 1.32  # ... and NEVER more than this
STRAIGHT_2UP_MIN = 1.30  # 2UP pays at least this ...
STRAIGHT_2UP_MAX = 1.50  # ... and NEVER more than this
# If the target cannot be reached (e.g. 100 odds), the ticket is topped up with
# "Home or Away" (12) picks where a draw is unlikely.
DOUBLE12_MAX_DRAW = 0.27  # 12 is only used when the draw chance is at most this
DOUBLE12_MIN_ODD = 1.15
DOUBLE12_MAX_ODD = 1.45
SOONER_BONUS = 0.012  # small push towards games that kick off sooner

# Replacements for handicap: "sure" picks that SportyBet prices clearly
FH_GOALS_MIN_ODD = 1.30  # "Over 0.5 goals (1st half)" must pay at least this much
DOUBLE12_SURE_DRAW = 0.24  # in normal tickets "Home or Away" (12) is used only when a draw is this unlikely

# MIXED TICKETS (every ticket except Straight win): only these pick types, and only strong ones.
# 1UP, 2UP, win either half, Home / Away to score 0.5,
# Home or Away (12), 2nd half Over 0.5 goals, Over 1.5 goals.
STRONG_MIN_P = 0.66  # a pick needs at least this chance (from SportyBet's prices / football stats)
SH_MIN_ODD = 1.15  # 2nd half picks pay at least this much

# Stricter strength for the Top leagues / European cups buttons, and for Daily 2 odds.
# (Without football data a pick's chance is about 0.95 / odds, so 0.70 means odds up to about 1.35.)
STRONG_MIN_P_TOP = STRONG_MIN_P  # Top leagues / cups: same strength as chat (raise it only if you want stricter)
DAILY_MIN_P = 0.72  # Daily 2 odds: strong picks only (lower to 0.70 if it finds nothing)

# Parts of the day (local time). Hours past 24 mean the next morning.
NIGHT_START_HOUR = 21
NIGHT_END_HOUR = 5
TODAY_END_HOUR = 20  # "today" games stop at 8pm
DAY_PARTS = {
    "morning": (5, 12),  # 5am to 12pm
    "afternoon": (12, 17),  # 12pm to 5pm
    "evening": (17, 21),  # 5pm to 9pm
    "night": (NIGHT_START_HOUR, 24 + NIGHT_END_HOUR),  # tonight = 9pm to 5am
    "midnight": (22, 24 + NIGHT_END_HOUR),  # 10pm to 5am
}

# U20 / U21 / youth and women's leagues are allowed in tickets (friendlies and reserve sides stay out).
# Set to False to skip them again.
ALLOW_YOUTH_WOMEN = True

# "Are there any midnight games?" -> the bot answers, then waits for "list" or "build".
LIST_MAX = 80  # a full list shows at most this many games (top leagues first)
PENDING_SECONDS = 15 * 60  # how long the bot remembers the question

# European cups (own card): picks when the person gives no number
CUPS_PICKS = 6

# Tight games (for example home 2.06, away 3.23): no win picks
TIGHT_MAX_P = 0.50  # neither team has more than this chance to win ...
TIGHT_MIN_P = 0.22  # ... and both have at least this chance

# Top leagues button
TOP_LEAGUE_PICKS = 8  # picks on the ticket when the person does not say a number

# Corner settings (only used when NO_CORNERS = False)
CORNERS_LINE = 7.5  # total corners are always "Over 7.5 corners"
CORNERS_TOTAL_MIN_ODD = 1.15  # Over 7.5 corners usually pays a little less than other picks
CORNERS_MIN_ODD = 1.30  # team corner picks pay at least this much
CORNERS_MAX_ODD = 2.30  # and at most this much
CORNERS_MIN_P = 0.60  # chance (from SportyBet's prices) a corner pick must have
CORNERS_PER_MATCH = 3  # how many corner choices each match offers the ticket builder
CORNERS_DETAIL = 100  # how many top-league matches get their corner prices read
CORNERS_MAX_PER_LEAGUE = 5  # corners tickets may use more matches from one league

# Individual team goals ("Home to score 0.5") are added to normal tickets
TEAM_GOALS_MIN_ODD = 1.25  # "Home / Away to score 0.5" must pay at least this (1.25 and above)
TEAM_GOALS_BONUS = 0.10  # how strongly the ticket builder prefers them (0 = no push)
TEAM_GOALS_SHARE = 0.35  # at most this share of a ticket may be team-goal picks

# Turn this on to let the ticket builder use every SportyBet market it knows
# (direct win, BTTS, Over goals, handicap, draw no bet ...), still strong picks only.
# Off = only the mixed-ticket picks listed above (1UP, 2UP, either half ...).
ALLOW_ALL_MARKETS = False

# EXTRA MARKETS: combos (GG or Over 2.5, Home / Away or GG, Home / Away or Draw & Over 1.5),
# and goal bounds. Full-time markets only.
# Every pick still has to pass the strong-pick check (STRONG_MIN_P), except goal bounds (BOUNDS_MIN_P).
EXTRA_KINDS = {"combo", "bounds", "halves", "fh_ft", "draw10", "either_half"}
EXTRA_FLOOR_ODD = 1.08  # lowest price these picks may have (each type also has its own range below)
COMBO_MIN_ODD = 1.20
COMBO_MAX_ODD = 1.50
COMBOS_PER_MATCH = 2  # how many combo choices each match offers the ticket builder
STREAK_MIN_ODD = 1.15  # "Any team to score 3+ goals in a row" (only used when NO_STREAK = False)
STREAK_MAX_ODD = 1.60
BOUNDS_MIN_ODD = 1.15  # a wide range (for example Home team 0-2) can pay quite low
BOUNDS_MAX_ODD = 2.60
BOUNDS_MIN_P = 0.60  # goal bounds use their own strength check
BOUNDS_WANTED = None  # None = any range the market offers, or a set such as {"0-1", "2-3", "3-4"}
BOUNDS_PER_MATCH = 2  # total goal bounds, Home team goal bounds and Away team goal bounds: at most this many per match
HALVES_MIN_ODD = 1.15  # Both halves Over 1.5 / Under 1.5 (yes or no)
HALVES_MAX_ODD = 1.60
FHFT_MIN_ODD = 1.20  # 1st half Over 0.5 & full-time Over 1.5
FHFT_MAX_ODD = 1.60
DRAW10_MIN_ODD = 1.10  # draw after 10 minutes (10 min 1X2)
DRAW10_MAX_ODD = 1.45
EITHER_HALF_MIN_ODD = 1.20  # Home / Away team to win either half
EITHER_HALF_MAX_ODD = 1.60
CORNERS_SHARE_NORMAL = 0.15  # (only used when NO_CORNERS = False)


# ------------------------------------------------------------
# 4) TOP LEAGUES (14 competitions) and EUROPEAN CUPS
# ------------------------------------------------------------
# Each rule is (country word, league-name pattern).
# The country word must appear in SportyBet's country name for that match.
# An empty country word is used for the European cups.

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
    ("england", r"^(efl |sky bet )?championship$"),
    ("saudi", r"^(roshn )?(saudi )?(professional |pro )?league$"),
    ("denmark", r"^(3f |danish )?super ?(lig(a|aen)?|league)$"),
]

# The European cups have their own card. Each can be chosen alone.
CUP_RULES = {
    "champions": r"^(uefa )?champions league(?:[ ,:;].*)?$",
    "europa": r"^(uefa )?europa league(?:[ ,:;].*)?$",
    "conference": r"^(uefa )?(europa )?conference league(?:[ ,:;].*)?$",
}

_CUP_COMPILED = {key: re.compile(pattern) for key, pattern in CUP_RULES.items()}

CUPS_RE = re.compile(r"\b(euro(?:pean)?\s*cups?|champions\s*league|europa\s*league|conference\s*league)\b", re.I)


def cups_from_text(text):
    """Which cups did the person ask for? Nothing named = all three."""
    low = str(text or "").lower()
    chosen = set()
    if "champions" in low:
        chosen.add("champions")
    if "conference" in low:
        chosen.add("conference")
    if re.search(r"europa(?!\s+conference)", low):
        chosen.add("europa")
    return chosen or {"champions", "europa", "conference"}


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


def is_cup_event(event, wanted):
    sport = event.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    name = _clean(tournament.get("name"))

    if not name or _NOT_TOP.search(name):
        return False

    return any(_CUP_COMPILED[key].search(name) for key in wanted if key in _CUP_COMPILED)


# ------------------------------------------------------------
# 5) Windows, straight win, weekend, football note
# ------------------------------------------------------------
WINDOW_RE = re.compile(
    r"\b(today|tonight|tomorrow|morning|afternoon|evening|night|midnight|weekend|weekdays?|midweek|week|\d+\s*days?)\b",
    re.I,
)
WEEKDAYS_RE = re.compile(r"\b(weekdays?|midweek)\b", re.I)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:odds?|picks?|games?|matches|selections?|tips?|legs?)\b", re.I)
# "straight win", "2up win", "1 up win" and similar all mean a Straight win ticket
STRAIGHT_RE = re.compile(r"\bstraight\s*-?\s*(?:win|winning|wins)\b|\b[12]\s*-?\s*up\b", re.I)
# "30 odds" (the odds the person wants), "win either half", and the word "today"
ODDS_RE = re.compile(r"\d+(?:\.\d+)?\s*odds?\b", re.I)
EITHER_RE = re.compile(r"either\s*-?\s*half", re.I)
TODAY_ONLY_RE = re.compile(r"\btoday\b", re.I)
QUESTION_START_RE = re.compile(r"^(what|why|how|explain|is|are|does|do|can|tell|who|when)\b", re.I)
WEEKEND_RE = re.compile(r"\bweekend\b", re.I)
DAYS_RE = re.compile(r"\d+\s*days?\b", re.I)
STRAIGHT_BUTTON_RE = re.compile(r"^straight\s*-?\s*wins?(\s+tickets?)?$", re.I)
PART_RE = re.compile(r"\b(morning|afternoon|evening|tonight|night|midnight)\b", re.I)
TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
TODAY_RE = re.compile(r"\b(today|this|tonight)\b", re.I)
PHRASE_RE = re.compile(r"\b(tomorrow|today|tonight|morning|afternoon|evening|night|midnight)\b", re.I)
GAMES_WORD_RE = re.compile(r"\b(games?|matches|match|fixtures?)\b", re.I)
BUILD_WORDS_RE = re.compile(
    r"\b(odds?|tickets?|build|picks?|safe|safer|safest|risky|code|acca|accumulator|predict\w*|banker|make|give)\b",
    re.I,
)
AVAIL_ASK_RE = re.compile(r"^(are|is|any|do|does|got|have|how many|what|which)\b|\bany\b|\?\s*$", re.I)
LIST_ASK_RE = re.compile(r"\b(list|show|fixtures?|schedule)\b", re.I)
LIST_REPLY_RE = re.compile(r"\b(list|show|full|all|fixtures?)\b", re.I)
BUILD_REPLY_RE = re.compile(r"\b(build|make|ticket|odds?|picks?|safe|predict\w*|give|acca)\b", re.I)


def _day_parts(low):
    """Which parts of the day were named: morning, afternoon, evening, night, midnight."""
    found = []
    for word in PART_RE.findall(low):
        key = "night" if word in ("tonight", "night") else word
        if key not in found:
            found.append(key)
    if "night" in found and "midnight" in found:
        found.remove("midnight")  # night (9pm to 5am) already covers midnight
    return found


def _h12(hour):
    h = int(hour) % 24
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def _has_window(low):
    return bool(PART_RE.search(low) or TOMORROW_RE.search(low) or re.search(r"\btoday\b", low))


def _window_phrase(low):
    words = []
    for w in PHRASE_RE.findall(low):
        w = w.lower()
        if w not in words:
            words.append(w)
    return " ".join(words)


SAFE_WORD_RE = re.compile(r"\b(safe|safer|safest|risky)\b", re.I)


if smart_ticket is not None:
    import sportybet_provider as sp

    st = smart_ticket
    st.STRAIGHT_RE = STRAIGHT_RE  # the ticket builder understands the same words

    if ALLOW_YOUTH_WOMEN:
        # Let U20 / U21 / youth and women's games through. Friendlies and reserve / academy sides stay out.
        st.SKIP_LEAGUE_RE = re.compile(r"friendl", re.I)
        st._UNRELIABLE_RE = re.compile(r"friendl|reserve|academy|development", re.I)

    # The bot may now look a full week ahead and read more SportyBet pages.
    bot.MAX_DAYS_AHEAD = max(getattr(bot, "MAX_DAYS_AHEAD", 2), SEARCH_DAYS)
    sp.MAX_EVENT_PAGES = max(getattr(sp, "MAX_EVENT_PAGES", 15), EVENT_PAGES)

    # ---- Memory safety: do not keep every match ever read ----
    def _trim(cache, limit):
        try:
            extra = len(cache) - limit
            if extra <= 0:
                return
            items = list(cache.items())
            items.sort(key=lambda kv: kv[1][0] if isinstance(kv[1], tuple) else 0)
            for key, _ in items[:extra]:
                cache.pop(key, None)
        except Exception:
            pass

    _orig_markets_cached = sp.SportyBetProvider._event_markets_cached

    def _event_markets_cached(self, event_id):
        result = _orig_markets_cached(self, event_id)
        _trim(self._up_cache, CACHE_LIMIT)
        return result

    sp.SportyBetProvider._event_markets_cached = _event_markets_cached

    if hasattr(smart_ticket, "study"):
        _orig_study = smart_ticket.study

        def study(*args, **kwargs):
            if getattr(CTX, "corners", False):
                return 0  # no corner statistics exist in the football data, so use the prices
            try:
                result = _orig_study(*args, **kwargs)

                # "Home or Away" and the extra markets have no match stats behind them: keep their price-based chance.
                try:
                    for group in (args[0] if args else []):
                        for c in group:
                            if c.get("filler") or c.get("corner_kind") or c.get("extra"):
                                c["p"] = c.get("market_p", c.get("p", 0))
                except Exception:
                    pass

                return result
            finally:
                _trim(getattr(smart_ticket, "_study_cache", {}), CACHE_LIMIT)

        smart_ticket.study = study

    smart_ticket.MAX_DETAIL_EVENTS = MAX_DETAIL
    smart_ticket.DETAIL_WORKERS = DETAIL_WORKERS

    # Individual team goals show up in more tickets.
    smart_ticket.KIND_BONUS["team_goals"] = max(smart_ticket.KIND_BONUS.get("team_goals", 0), TEAM_GOALS_BONUS)
    smart_ticket.KIND_CAP["team_goals"] = max(smart_ticket.KIND_CAP.get("team_goals", 0), TEAM_GOALS_SHARE)
    smart_ticket.KIND_CAP["corners"] = CORNERS_SHARE_NORMAL  # (corners are switched off by NO_CORNERS anyway)
    smart_ticket.KIND_CAP["dc12"] = 0.25
    smart_ticket.KIND_CAP["sh_goals"] = 0.25
    smart_ticket.KIND_CAP["sh_12"] = 0.25
    smart_ticket.KIND_BONUS["sh_goals"] = 0.05
    smart_ticket.KIND_BONUS["sh_12"] = 0.03
    smart_ticket.KIND_BONUS["dc12"] = 0.02
    for _kind, _cap in (
        ("combo", 0.30), ("streak", 0.25), ("bounds", 0.25),
        ("halves", 0.20), ("fh_ft", 0.20), ("draw10", 0.15), ("either_half", 0.30),
    ):
        smart_ticket.KIND_CAP[_kind] = _cap
        smart_ticket.KIND_BONUS[_kind] = 0.04
    try:
        smart_ticket.KIND_NAME["sh_goals"] = "2nd half goals"
        smart_ticket.KIND_NAME["sh_12"] = "2nd half Home or Away (12)"
        smart_ticket.KIND_NAME["dc12"] = "Home or Away (12)"
        smart_ticket.KIND_NAME["combo"] = "combo (GG / Over)"
        smart_ticket.KIND_NAME["streak"] = "3 goals in a row"
        smart_ticket.KIND_NAME["bounds"] = "goal bounds"
        smart_ticket.KIND_NAME["halves"] = "both halves"
        smart_ticket.KIND_NAME["fh_ft"] = "1st half + full-time goals"
        smart_ticket.KIND_NAME["draw10"] = "10 minutes draw"
        smart_ticket.KIND_NAME["either_half"] = "win either half"
    except Exception:
        pass

    # ---- Top leagues (14 competitions) or the chosen European cups: filter BEFORE the matches are studied ----
    _orig_get_upcoming = sp.SportyBetProvider.get_upcoming

    def get_upcoming(self, start, end):
        events = _orig_get_upcoming(self, start, end)

        cups = getattr(CTX, "cups", None)

        if cups:
            kept = [e for e in events if is_cup_event(e, cups)]
            print(f"European cups {sorted(cups)}: kept {len(kept)} of {len(events)} matches")

            if not kept and events:
                seen = set()
                for e in events:
                    cat = ((e.get("sport") or {}).get("category") or {})
                    name = str((cat.get("tournament") or {}).get("name"))
                    if re.search(r"champions|europa|conference", name, re.I):
                        seen.add(f"{cat.get('name')} / {name}")
                print("European cups: nothing matched. Cup-like names seen:", sorted(seen)[:20])

            return kept

        if not getattr(CTX, "top_leagues", False):
            return events

        kept = [e for e in events if is_top_league(e)]

        try:
            counts = {}
            for e in kept:
                name = str((((e.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name"))
                counts[name] = counts.get(name, 0) + 1
            print(f"Top leagues: kept {len(kept)} of {len(events)} matches: {counts}")
        except Exception:
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

    def _buffer_hours():
        """Games starting within this many hours are skipped. Zero when only checking what exists."""
        if getattr(CTX, "availability", False):
            return 0
        return getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1)

    # ---- Windows: parts of the day, tomorrow = full day, weekend = Fri to Sun, straight win = a week ----
    _orig_parse_request = bot.parse_request

    def parse_request(text):
        req = _orig_parse_request(text)
        low = str(text or "").lower()
        now = datetime.now(timezone.utc)

        parts = _day_parts(low)

        if parts and not DAYS_RE.search(low):
            # morning / afternoon / evening / tonight / midnight: only that part of the day.
            local_now = now.astimezone(bot.LOCAL_TZ)
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            earliest = now + timedelta(hours=_buffer_hours())

            first = min(DAY_PARTS[p][0] for p in parts)
            last = max(DAY_PARTS[p][1] for p in parts)

            tomorrow = bool(TOMORROW_RE.search(low))
            today_word = bool(TODAY_RE.search(low))

            day = 1 if tomorrow else 0

            if not tomorrow and first >= NIGHT_START_HOUR and local_now.hour < NIGHT_END_HOUR:
                day = -1  # already after midnight: use the night that began yesterday

            start = midnight + timedelta(days=day, hours=first)
            end = midnight + timedelta(days=day, hours=last)

            if not tomorrow and not today_word and end <= earliest:
                # That part of today is over (for example "morning" at 8pm): use tomorrow's.
                start += timedelta(days=1)
                end += timedelta(days=1)

            req["start"], req["end"] = min(max(earliest, start), end), end

            names = [("tonight" if p == "night" and not tomorrow else p) for p in parts]
            req["label"] = (
                ("tomorrow " if tomorrow else "")
                + " & ".join(names)
                + f" ({_h12(first)} to {_h12(last)})"
            )
            req["exact_window"] = True

        elif (
            TOMORROW_RE.search(low)
            and not DAYS_RE.search(low)
            and not WEEKEND_RE.search(low)
            and not WEEKDAYS_RE.search(low)
        ):
            # "tomorrow" alone = the whole day (12am to 12am).
            local_now = now.astimezone(bot.LOCAL_TZ)
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = midnight + timedelta(days=1)

            req["start"], req["end"] = start, start + timedelta(days=1)
            req["label"] = "tomorrow (full day)"

            if getattr(CTX, "straight_extend", False):
                # straight win with odds: tomorrow first, then the next days if the odds are not reached
                req["end"] = start + timedelta(days=SEARCH_DAYS - 1)
                req["label"] = "tomorrow, then the next days if needed"
            req["exact_window"] = True

        elif WEEKDAYS_RE.search(low) and not DAYS_RE.search(low):
            # Weekdays = Monday to Thursday (midweek games, European cups).
            local_now = now.astimezone(bot.LOCAL_TZ)
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            weekday = local_now.weekday()
            earliest = now + timedelta(hours=getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1))

            start, end = earliest, midnight + timedelta(days=4 - weekday)

            if weekday >= 4 or (end - start) < timedelta(hours=12):
                # Friday to Sunday (or too little of this week left): next Monday to Thursday.
                monday = midnight + timedelta(days=7 - weekday)
                start, end = monday, monday + timedelta(days=4)

            req["start"], req["end"] = start, end
            req["label"] = "weekdays (Mon to Thu)"

        elif WEEKEND_RE.search(low) and not DAYS_RE.search(low):
            local_now = now.astimezone(bot.LOCAL_TZ)
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            weekday = local_now.weekday()  # Monday = 0 ... Sunday = 6
            earliest = now + timedelta(hours=getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1))

            if weekday <= 4:  # Monday to Friday: next Friday to Sunday
                friday = midnight + timedelta(days=4 - weekday)
                start, end = max(earliest, friday), friday + timedelta(days=3)
            elif weekday == 5:  # Saturday: Saturday and Sunday
                start, end = max(earliest, midnight), midnight + timedelta(days=2)
            else:  # Sunday: today only
                start, end = max(earliest, midnight), midnight + timedelta(days=1)

            req["start"], req["end"] = start, end
            req["label"] = "this weekend (Fri to Sun)"

        elif re.search(r"\btoday\b", low) and not DAYS_RE.search(low):
            # "today" alone = from now until 8pm only (no late 10pm / 11pm games).
            local_now = now.astimezone(bot.LOCAL_TZ)
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            earliest = now + timedelta(hours=_buffer_hours())
            end = midnight + timedelta(hours=TODAY_END_HOUR)

            if getattr(CTX, "straight_extend", False):
                # "today's straight win 30 odds": look at today AND tomorrow. choose_straight uses
                # today's games first and only adds tomorrow's if the odds cannot be reached.
                req["start"], req["end"] = earliest, midnight + timedelta(days=SEARCH_DAYS)
                req["label"] = "today, then the next days if needed"
                req["exact_window"] = True
            elif end > earliest:
                req["start"], req["end"] = earliest, end
                req["label"] = f"today (until {_h12(TODAY_END_HOUR)})"
                req["exact_window"] = True  # never borrow games from other days
            # after 8pm there is nothing left in the window: the normal "today" is kept

        elif STRAIGHT_RE.search(low) and not WINDOW_RE.search(low):
            req["end"] = req["start"] + timedelta(days=SEARCH_DAYS)
            req["label"] = f"next {SEARCH_DAYS} days"

        return req

    bot.parse_request = parse_request

    # ---- Straight win: strong top-league teams, 1UP or 2UP only, with price limits ----
    # 55% to 59% chance to win -> 1UP (price 1.20 to 1.32, never above 1.32)
    # 60% or more -> 2UP (price 1.30 to 1.50, never above 1.50)
    # If the person also asks for "win either half", those picks are offered next to the 1UP / 2UP picks.
    def straight_up_candidates(event, markets):
        h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
        d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
        a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

        if not (h and d and a):
            return []

        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)

        if not total:
            return []

        chances = {"home": inv[0] / total, "away": inv[2] / total}

        ups = [c for c in st.event_candidates(event, markets) if c.get("kind") == "up"]

        out = []

        for side in ("home", "away"):
            strength = chances[side]

            if strength < STRAIGHT_STRONG_P:
                continue

            mine = [c for c in ups if c.get("side") == side]
            up1 = next((c for c in mine if c.get("up_level") == 1), None)
            up2 = next((c for c in mine if c.get("up_level") == 2), None)

            ok1 = up1 if (up1 and STRAIGHT_1UP_MIN <= up1.get("odd", 0) <= STRAIGHT_1UP_MAX) else None
            ok2 = up2 if (up2 and STRAIGHT_2UP_MIN <= up2.get("odd", 0) <= STRAIGHT_2UP_MAX) else None

            if strength >= STRAIGHT_2UP_FROM_P:
                pick = ok2 or ok1
            else:
                pick = ok1 or ok2

            if pick:
                pick = dict(pick)
                pick["market_side_p"] = strength
                pick["side"] = side
                out.append(pick)

        if getattr(CTX, "straight_either", False):
            # "straight win and win either half": Home / Away to win either half joins the 1UP / 2UP picks.
            try:
                out += either_half_candidates(event, markets, [], False)
            except Exception as exc:
                print(f"Straight win either-half picks failed: {exc}")

        # Straight win is ONLY 1UP / 2UP (and win either half when asked): no "Home or Away" (12) top-up.
        return out

    def choose_straight(groups, target, count):
        now = datetime.now(timezone.utc)

        best = []

        for group in groups:
            usable = [c for c in group if c.get("p", 0) > 0]

            if not usable:
                continue

            main = [c for c in usable if not c.get("filler")]
            best.append(max(main or usable, key=lambda c: c.get("p", 0)))

        def score(candidate):
            days = max(0.0, (candidate["kickoff"] - now).total_seconds() / 86400)
            return candidate.get("p", 0) - SOONER_BONUS * days

        if getattr(CTX, "straight_extend", False):
            # Fill the ticket day by day: the first day's games come first, and the next day
            # is only used when the odds asked for are not reached with the day before.
            local_now = now.astimezone(bot.LOCAL_TZ)
            local_today = local_now.date()
            today_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=TODAY_END_HOUR)

            def day_key(candidate):
                local_kick = candidate["kickoff"].astimezone(bot.LOCAL_TZ)
                day = local_kick.date()

                # "today" means until 8pm: later games count with tomorrow
                if getattr(CTX, "straight_today", False) and day == local_today and candidate["kickoff"] > today_end:
                    day = local_today + timedelta(days=1)

                return day

            best.sort(key=lambda c: (
                1 if c.get("filler") else 0,
                day_key(c),
                -score(c),
            ))
        else:
            # 1UP / 2UP picks first. "Home or Away" only fills what is still missing.
            best.sort(key=lambda c: (1 if c.get("filler") else 0, -score(c)))

        if not best:
            return [], False

        if count:
            count = min(int(count), st.MAX_LEGS)
            return best[:count], len(best) >= count

        if target:
            chosen, total = [], 1.0

            for candidate in best[:st.MAX_LEGS]:
                chosen.append(candidate)
                total *= candidate["odd"]

                if total >= target:
                    return chosen, True

            return chosen, False

        return best[:st.STRAIGHT_DEFAULT_LEGS], True

    st.straight_candidates = straight_up_candidates
    st.choose_straight = choose_straight
    st.STRAIGHT_DEFAULT_LEGS = STRAIGHT_LEGS
    st.STRAIGHT_MIN_P = STRAIGHT_MIN_P
    st.MAX_LEGS = max(getattr(st, "MAX_LEGS", 0), STRAIGHT_MAX_LEGS)  # never lower than before

    # ---- Remember how many matches really got football stats (shown under the ticket) ----
    _orig_build_ticket = st.build_ticket

    def _odds_of(built):
        total = 1.0
        for c in built.get("chosen") or []:
            total *= c.get("odd", 1.0)
        return total

    def build_ticket(*args, **kwargs):
        built = _orig_build_ticket(*args, **kwargs)

        # A target (for example 100 odds) that the normal picks cannot reach: top the ticket up
        # with "Home or Away" (12) where a draw is unlikely.
        try:
            target = args[2] if len(args) > 2 else kwargs.get("target")

            if (
                target
                and not built.get("reached")
                and not kwargs.get("daily")
                and not getattr(CTX, "corners", False)
                and not getattr(CTX, "straight", False)  # straight win never gets 12 picks
                and not ONLY_UP_AND_EITHER_HALF  # nothing else may be added
            ):
                CTX.fallback = True

                try:
                    trial = _orig_build_ticket(*args, **kwargs)
                finally:
                    CTX.fallback = False

                if trial.get("reached") or _odds_of(trial) > _odds_of(built):
                    extra = (
                        f"To get closer to {target:g} odds I added "
                        "'Home or Away' (12) picks where a draw is unlikely."
                    )
                    trial["note"] = ((trial.get("note") or "") + " " + extra).strip()
                    built = trial
        except Exception as exc:
            print(f"Top-up with 12 picks failed: {exc}")

        try:
            CTX.football_note = (
                built.get("studied") or 0,
                built.get("events") or 0,
                len(built.get("chosen") or []),
            )
        except Exception:
            pass

        return built

    st.build_ticket = build_ticket

    # ---- Top leagues: list every game of the 14 leagues or chosen cups ----
    ASKS_FOR_TICKET_RE = re.compile(
        r"\b(safe|safer|safest|risky|ticket|code|pick|picks|odd|odds|acca|accumulator|straight|banker|bankers)\b",
        re.I,
    )

    LIST_RE = re.compile(r"\b(games|fixtures|matches|list|show|schedule)\b", re.I)

    def _top_rank(event):
        sport = event.get("sport") or {}
        category = sport.get("category") or {}
        tournament = category.get("tournament") or {}
        country = _clean(category.get("name"))
        name = _clean(tournament.get("name"))

        for index, (need, pattern) in enumerate(_TOP_COMPILED):
            if need and need not in country:
                continue
            if pattern.search(name):
                return index

        return 99

    def top_leagues_list(chat_id, text):
        provider = getattr(bot, "SPORTYBET_PROVIDER", None)
        title = "EUROPEAN CUPS" if getattr(CTX, "cups", None) else "TOP LEAGUES"

        if provider is None:
            bot.send_message(chat_id, "\u274C SportyBet mode is off on this server.")
            return

        if not WINDOW_RE.search(text):
            text += " this week"

        req = bot.parse_request(text)

        try:
            events = provider.get_upcoming(req["start"], req["end"])
        except Exception as exc:
            print(f"Top leagues list failed: {exc}")
            bot.send_message(
                chat_id,
                "\u274C I couldn't read SportyBet right now. Please try again in a minute.",
            )
            return

        rows = []

        for event in events:
            ms = event.get("estimateStartTime")

            if not ms:
                continue

            kickoff = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(bot.LOCAL_TZ)

            category = ((event.get("sport") or {}).get("category") or {})
            tournament = category.get("tournament") or {}
            country = str(category.get("name") or "")
            name = str(tournament.get("name") or "League")

            if not country or re.search(r"international|europe|uefa", country, re.I):
                label = name
            else:
                label = f"{name} ({country})"

            rows.append((
                kickoff.date(),
                _top_rank(event),
                label,
                kickoff,
                str(event.get("homeTeamName") or "Home"),
                str(event.get("awayTeamName") or "Away"),
            ))

        if not rows:
            bot.send_message(
                chat_id,
                f"\u26BD <b>{title}</b>\n\n"
                f"I couldn't find top league games for {escape(req['label'])}. "
                "Try again a bit later.",
            )
            return

        rows.sort(key=lambda r: (r[0], r[1], r[3]))

        lines = [
            f"\u26BD <b>{title}</b>",
            f"\U0001F4C5 {escape(str(req['label']).capitalize())} - times in {bot.LOCAL_TZ_NAME}",
            f"\U0001F4CA {len(rows)} match" + ("es" if len(rows) != 1 else ""),
        ]

        current_day = None
        current_league = None
        number = 0

        for day, rank, label, kickoff, home, away in rows:
            if day != current_day:
                current_day = day
                current_league = None
                lines.append("")
                lines.append(f"\U0001F4C6 <b>{escape(kickoff.strftime('%A, %d %b'))}</b>")

            if label != current_league:
                current_league = label
                lines.append("")
                lines.append(f"\U0001F3C6 <b>{escape(label)}</b>")

            number += 1
            lines.append(f"{number}. {kickoff.strftime('%H:%M')} - {escape(home)} vs {escape(away)}")

        bot.send_message(chat_id, "\n".join(lines))

    # ---- Straight win: read the 1UP / 2UP prices of the RIGHT matches ----
    # The 1UP / 2UP prices only exist in each match's full list of markets, and only
    # a limited number of matches can be read. Straight win needs teams with a 55% to
    # 85% chance to win (the strongest teams pay too little), spread across the days.
    _orig_detail_order = st._detail_order

    def _detail_order(events):
        if getattr(CTX, "corners", False) or (
            getattr(CTX, "fallback", False) and getattr(CTX, "straight", False)
        ):
            ordered = sorted(events, key=lambda e: e.get("estimateStartTime") or 0)
            return ordered[:CORNERS_DETAIL]

        if not getattr(CTX, "straight", False):
            return _orig_detail_order(events)

        band = []

        # "win either half" is priced by its own odds, so weaker favourites can be read too.
        floor = 0.45 if getattr(CTX, "straight_either", False) else STRAIGHT_STRONG_P

        for event in events:
            try:
                strength = st.favourite_strength(event)
            except Exception:
                continue

            if floor <= strength <= STRAIGHT_MAX_STRENGTH:
                band.append((strength, event))

        if getattr(CTX, "straight_extend", False):
            # "today's straight win 30 odds": read today's games first, then tomorrow's.
            band.sort(key=lambda item: item[1].get("estimateStartTime") or 0)
            return [event for _, event in band[:STRAIGHT_DETAIL]]

        band.sort(key=lambda item: -item[0])

        if len(band) <= STRAIGHT_DETAIL:
            return [event for _, event in band]

        # Spread evenly over the whole band so every strength level is covered.
        step = len(band) / STRAIGHT_DETAIL
        return [band[int(i * step)][1] for i in range(STRAIGHT_DETAIL)]

    st._detail_order = _detail_order

    _orig_gather = st.gather

    def gather(*args, **kwargs):
        mode_normal = not (
            getattr(CTX, "corners", False)
            or getattr(CTX, "straight", False)
            or kwargs.get("daily")
            or kwargs.get("straight")
        )

        original_floor = None

        # Team goals (Home / Away to score 0.5) usually pay 1.15 to 1.40, below the normal
        # minimum, so they were being thrown out. Let them in, then keep the normal minimum
        # for every other kind of pick.
        if mode_normal and len(args) >= 5 and isinstance(args[4], (int, float)):
            original_floor = float(args[4])
            args = list(args)
            args[4] = min(original_floor, TEAM_GOALS_MIN_ODD, STRAIGHT_1UP_MIN, CORNERS_TOTAL_MIN_ODD, DOUBLE12_MIN_ODD, SH_MIN_ODD, EXTRA_FLOOR_ODD)

        result = _orig_gather(*args, **kwargs)

        if original_floor is not None:
            try:
                groups = []
                for group in result[0]:
                    kept = [
                        c for c in group
                        if (
                            (c.get("kind") == "team_goals" and c.get("odd", 0) >= TEAM_GOALS_MIN_ODD)
                            or (c.get("kind") == "up" and c.get("up_level") == 1 and c.get("odd", 0) >= STRAIGHT_1UP_MIN)
                            or (c.get("kind") == "corners" and c.get("odd", 0) >= CORNERS_TOTAL_MIN_ODD)
                            or (c.get("kind") == "dc12" and c.get("odd", 0) >= DOUBLE12_MIN_ODD)
                            or (c.get("kind") in ("sh_goals", "sh_12") and c.get("odd", 0) >= SH_MIN_ODD)
                            or c.get("kind") in EXTRA_KINDS
                            or c.get("odd", 0) >= original_floor
                        )
                    ]
                    if kept:
                        groups.append(kept)
                result = (groups,) + tuple(result[1:])
            except Exception:
                pass

        # Mixed tickets and Daily 2 odds: strong picks only.
        if mode_normal or kwargs.get("daily"):
            try:
                if kwargs.get("daily"):
                    min_p = DAILY_MIN_P
                elif getattr(CTX, "top_leagues", False) or getattr(CTX, "cups", None):
                    min_p = STRONG_MIN_P_TOP
                else:
                    min_p = STRONG_MIN_P

                groups = []
                for group in result[0]:
                    kept = [
                        c for c in group
                        if c.get("p", 0) >= (BOUNDS_MIN_P if c.get("kind") == "bounds" else min_p)
                    ]
                    if kept:
                        groups.append(kept)

                counts = {}
                for group in groups:
                    for c in group:
                        counts[c.get("kind")] = counts.get(c.get("kind"), 0) + 1

                print(
                    f"Mix picks available (strong only, p>={min_p:g}): {counts} "
                    f"| 2nd-half markets seen: {sorted(getattr(CTX, 'half_names', set()))[:8]}"
                )
                CTX.half_names = set()

                print("Extra picks found (before the strong check):", dict(_EXTRA_COUNT))
                _EXTRA_COUNT.clear()

                if _EXTRA_NAMES:
                    print("Extra markets seen:", sorted(_EXTRA_NAMES)[:40])
                    _EXTRA_NAMES.clear()

                result = (groups,) + tuple(result[1:])
            except Exception:
                pass

        if getattr(CTX, "straight", False):
            try:
                groups, events, details, studied = result
                print(
                    f"Straight win: events={events} read_in_detail={details} "
                    f"studied={studied} matches_with_picks={len(groups)}"
                )
            except Exception:
                pass

        return result

    st.gather = gather

    # ---- Corners (switched off by NO_CORNERS) and individual team goals ----
    CORNERS_RE = re.compile(r"\bcorners?\b", re.I)

    _BAD_CORNER_WORDS = (
        "1st", "2nd", "first", "second", "half", "handicap", "1x2", "race",
        "odd", "even", "exact", "range", "both", "most",
    )

    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _pair_p(over, under):
        if not over or over <= 1:
            return None
        if under and under > 1:
            a, b = 1 / over, 1 / under
            return a / (a + b)
        return min(0.95 / over, 0.97)

    def _over_under(market):
        over = under = over_id = None
        for outcome in market.get("outcomes", []):
            if outcome.get("isActive") is False:
                continue
            label = str(outcome.get("desc") or outcome.get("name") or "").strip().lower()
            oid = str(outcome.get("id"))
            odd = _num(outcome.get("odds"))
            if label.startswith("over") or oid == sp.OUT_TOTAL["over"]:
                over, over_id = odd, oid
            elif label.startswith("under") or oid == sp.OUT_TOTAL["under"]:
                under = odd
        return over, under, over_id

    def _corner_kind(market, home, away):
        """'total' (whole match), 'home', 'away', or None."""
        spec = market.get("specifier") or ""
        if not spec.startswith("total="):
            return None
        if str(market.get("id")) == sp.M_CORNERS:
            return "total"
        name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()
        if "corner" not in name or any(w in name for w in _BAD_CORNER_WORDS):
            return None
        h, a = home.lower(), away.lower()
        has_home = "home" in name or (len(h) > 3 and h in name)
        has_away = "away" in name or (len(a) > 3 and a in name)
        if has_home and not has_away:
            return "home"
        if has_away and not has_home:
            return "away"
        return None

    def _centre(points):
        """The number of corners the market treats as a 50/50 (rough guide)."""
        points = sorted(points)
        for (l1, p1), (l2, p2) in zip(points, points[1:]):
            if p1 >= 0.5 >= p2 and p1 != p2:
                return l1 + (p1 - 0.5) / (p1 - p2) * (l2 - l1)
        return None

    def corners_candidates(event, markets):
        if NO_CORNERS:
            return []  # corners are switched off everywhere

        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        stats = getattr(CTX, "corner_stats", None)

        found, total_points, any_team = [], [], False

        for market in markets or []:
            name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()
            kind = _corner_kind(market, home, away)

            if stats is not None and "corner" in name and len(stats["names"]) < 25:
                stats["names"].add(f"{market.get('id')}: {name.strip()}")

            if not kind:
                continue

            spec = market.get("specifier") or ""
            line = _num(spec.replace("total=", ""))

            if line is None or (line * 2) % 1 != 0:
                continue

            over, under, over_id = _over_under(market)

            if not over:
                continue

            chance = _pair_p(over, under)

            if kind == "total":
                total_points.append((line, chance))
            else:
                any_team = True

            if kind == "total" and abs(line - CORNERS_LINE) > 1e-9:
                continue  # total corners are only ever Over 7.5

            floor = CORNERS_TOTAL_MIN_ODD if kind == "total" else CORNERS_MIN_ODD

            if not (floor <= over <= CORNERS_MAX_ODD) or chance < CORNERS_MIN_P:
                continue

            if kind == "total":
                label = f"Over {line:g} corners"
            else:
                label = f"{home if kind == 'home' else away} Over {line:g} corners"

            found.append({
                "kind": "corners",
                "label": label,
                "odd": over,
                "p": chance,
                "key": (str(market.get("id")), spec, over_id),
                "line": line,
                "corner_kind": kind,
            })

        if stats is not None and (found or total_points):
            stats["events"] += 1
            if any_team:
                stats["with_team"] += 1

        centre = _centre(total_points)
        for item in found:
            item["corner_centre"] = centre if item["corner_kind"] == "total" else None

        found.sort(key=lambda c: -c["p"])
        return found[:CORNERS_PER_MATCH]

    def team_goal_candidates(event, markets, existing):
        """Individual team goals: Home / Away to score 1+ (Over 0.5) or 2+ (Over 1.5)."""
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        have = {c.get("label") for c in existing}
        out = []

        for market in markets or []:
            mid = str(market.get("id"))

            if mid not in (sp.M_HOME_TEAM_GOALS, sp.M_AWAY_TEAM_GOALS):
                continue

            spec = market.get("specifier") or ""

            if not spec.startswith("total="):
                continue

            line = _num(spec.replace("total=", ""))

            if line != 0.5:
                continue  # only "Home to score 0.5" / "Away to score 0.5"

            over, under, over_id = _over_under(market)

            if not over or over < TEAM_GOALS_MIN_ODD:
                continue

            side = "home" if mid == sp.M_HOME_TEAM_GOALS else "away"
            team = home if side == "home" else away
            label = f"{team} to score {1 if line == 0.5 else 2}+ goals (Over {line:g})"

            if label in have:
                continue

            out.append({
                "kind": "team_goals",
                "label": label,
                "odd": over,
                "p": _pair_p(over, under),
                "key": (mid, spec, over_id),
                "side": side,
                "line": line,
            })

        return out

    def _is_tight(markets):
        """True when neither team is a clear favourite (for example home 2.06, away 3.23)."""
        h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
        d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
        a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

        if not (h and d and a):
            return False

        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)

        if not total:
            return False

        ph, pa = inv[0] / total, inv[2] / total
        return max(ph, pa) < TIGHT_MAX_P and min(ph, pa) >= TIGHT_MIN_P

    def _double12(event, markets, max_draw, filler):
        """Home or Away (12): only where the draw is unlikely and the price is small."""
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")

        h = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["home"]))
        d = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["draw"]))
        a = sp.find_odds(markets, (sp.M_1X2, "", sp.OUT_1X2["away"]))

        if not (h and d and a):
            return []

        inv = [1 / h, 1 / d, 1 / a]
        total = sum(inv)
        draw = inv[1] / total if total else 1.0
        key = (sp.M_DC, "", sp.OUT_DC["12"])
        odd12 = sp.find_odds(markets, key)

        if not odd12 or draw > max_draw or not (DOUBLE12_MIN_ODD <= odd12 <= DOUBLE12_MAX_ODD):
            return []

        item = {
            "kind": "dc12",
            "label": f"{home} or {away} (12)",
            "odd": odd12,
            "p": 1 - draw,
            "key": key,
            "draw_p": draw,
        }

        if filler:
            item["filler"] = True

        return [item]

    def second_half_candidates(event, markets, existing):
        """2nd half Over 0.5 goals, and 2nd half Home or Away (12)."""
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        have = {c.get("label") for c in existing}
        out = []
        goals_done = False
        dc_done = True  # 2nd half Home or Away (12) is switched off
        names = getattr(CTX, "half_names", None)

        if names is None:
            names = set()
            CTX.half_names = names

        for market in markets or []:
            name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()

            if not ("2nd half" in name or "second half" in name):
                continue

            if len(names) < 15:
                names.add(f"{market.get('id')}: {name.strip()}")

            if any(w in name for w in (
                "corner", "card", "booking", "handicap", "both", "odd", "even",
                "exact", "range", "1x2", "1st", "first half",
            )):
                continue

            spec = market.get("specifier") or ""

            if spec.startswith("total=") and "total" in name and not any(w in name for w in ("home", "away", "team")):
                if goals_done or _num(spec.replace("total=", "")) != 0.5:
                    continue

                over, under, over_id = _over_under(market)

                if over and over >= SH_MIN_ODD:
                    label = "Over 0.5 goals (2nd half)"
                    goals_done = True

                    if label not in have:
                        out.append({
                            "kind": "sh_goals",
                            "label": label,
                            "odd": over,
                            "p": _pair_p(over, under),
                            "key": (str(market.get("id")), spec, over_id),
                            "line": 0.5,
                        })

            elif "double chance" in name and not dc_done:
                for outcome in market.get("outcomes", []):
                    if outcome.get("isActive") is False:
                        continue

                    label_o = str(outcome.get("desc") or outcome.get("name") or "").strip().lower()
                    oid = str(outcome.get("id"))
                    odd = _num(outcome.get("odds"))
                    is_12 = oid == sp.OUT_DC["12"] or "home or away" in label_o or label_o in ("12", "1 or 2")

                    if is_12 and odd and odd >= SH_MIN_ODD:
                        label = f"{home} or {away} (2nd half 12)"
                        dc_done = True

                        if label not in have:
                            out.append({
                                "kind": "sh_12",
                                "label": label,
                                "odd": odd,
                                "p": min(0.95 / odd, 0.97),
                                "key": (str(market.get("id")), spec, oid),
                            })
                        break

        return out

    def first_half_candidates(event, markets, existing):
        """Over 0.5 goals in the first half (a replacement for the handicap picks)."""
        have = {c.get("label") for c in existing}
        out = []

        for market in markets or []:
            spec = market.get("specifier") or ""

            if not spec.startswith("total="):
                continue

            name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower()

            if not ("1st half" in name or "first half" in name):
                continue

            if "total" not in name or any(
                w in name for w in (
                    "corner", "card", "booking", "home", "away", "team", "both",
                    "odd", "even", "exact", "range", "handicap", "1x2", "2nd",
                )
            ):
                continue

            if _num(spec.replace("total=", "")) != 0.5:
                continue

            over, under, over_id = _over_under(market)

            if not over or over < FH_GOALS_MIN_ODD:
                continue

            label = "Over 0.5 goals (1st half)"

            if label in have:
                continue

            out.append({
                "kind": "fh_goals",
                "label": label,
                "odd": over,
                "p": _pair_p(over, under),
                "key": (str(market.get("id")), spec, over_id),
                "line": 0.5,
            })
            break

        return out

    def fallback_fillers(event, markets):
        """Extra picks, used only when the odds asked for cannot be reached."""
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        out = []

        # Corners are switched off (corners_candidates returns nothing when NO_CORNERS = True)
        try:
            for item in corners_candidates(event, markets)[:2]:
                item = dict(item)
                item["filler"] = True
                out.append(item)
        except Exception:
            pass

        # Home or Away (12) where a draw is unlikely
        out += _double12(event, markets, DOUBLE12_MAX_DRAW, True)

        return out

    # ---- Extra markets: combos, goal bounds, halves ... ----
    # SportyBet's exact market names are matched by their words. The server log line
    # "Extra markets seen: ..." shows the real names, so you can fix a pattern if one never appears.
    _EXTRA_NAMES = set()
    _EXTRA_COUNT = {}
    _HALF_WORDS = ("1st half", "2nd half", "first half", "second half")
    _GG = r"(?:gg|both teams(?: to score)?|btts)"

    _HOME = r"(?:home(?: team)?|1)"
    _AWAY = r"(?:away(?: team)?|2)"
    _SEP = r"\s*(?:or|/)\s*"

    # (rule name, label pattern, needs a clear favourite side, text shown on the ticket)
    _COMBO_RULES = [
        (
            "home_gg",
            re.compile(rf"^{_HOME}{_SEP}{_GG}\b|^{_GG}{_SEP}{_HOME}\b"),
            True,
            "{home} or GG",
        ),
        (
            "away_gg",
            re.compile(rf"^{_AWAY}{_SEP}{_GG}\b|^{_GG}{_SEP}{_AWAY}\b"),
            True,
            "{away} or GG",
        ),
        (
            "home_dc_o15",
            re.compile(r"^(?:home or draw|1x)\s*(?:&|and)\s*over 1\.5\b"),
            True,
            "{home} or Draw & Over 1.5",
        ),
        (
            "away_dc_o15",
            re.compile(r"^(?:away or draw|x2)\s*(?:&|and)\s*over 1\.5\b"),
            True,
            "{away} or Draw & Over 1.5",
        ),
    ]

    def _mname(market):
        return f"{market.get('desc') or ''} {market.get('name') or ''}".lower()

    def _olabel(outcome):
        return str(outcome.get("desc") or outcome.get("name") or "").strip().lower()

    def _live_outcomes(market):
        out = []
        for outcome in market.get("outcomes", []):
            if outcome.get("isActive") is False:
                continue
            odd = _num(outcome.get("odds"))
            if odd and odd > 1:
                out.append((outcome, odd))
        return out

    def _note_extra(market):
        if len(_EXTRA_NAMES) >= 60:
            return
        name = _mname(market)
        if re.search(r"\bgg\b|both teams|in a row|bound|&|10 ?min|either half|both halves", name):
            labels = ", ".join(_olabel(o) for o, _ in _live_outcomes(market)[:4])
            _EXTRA_NAMES.add(f"{market.get('id')}: {name.strip()} [{labels}]")

    def _norm_label(outcome, spec, home, away):
        label = _olabel(outcome).replace("\u2013", "-")
        h, a = home.lower(), away.lower()
        if len(h) > 2:
            label = label.replace(h, "home")
        if len(a) > 2:
            label = label.replace(a, "away")
        line = _num(spec.replace("total=", "")) if spec.startswith("total=") else None
        if line is not None and re.search(r"\b(over|under)\b(?!\s*\d)", label):
            label = re.sub(r"\b(over|under)\b", lambda m: f"{m.group(1)} {line:g}", label, count=1)
        return re.sub(r"\s+", " ", label).strip()

    def combo_candidates(event, markets, tight):
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        best = {}

        for market in markets or []:
            if any(w in _mname(market) for w in _HALF_WORDS):
                continue

            spec = market.get("specifier") or ""

            for outcome, odd in _live_outcomes(market):
                if not (COMBO_MIN_ODD <= odd <= COMBO_MAX_ODD):
                    continue

                label = _norm_label(outcome, spec, home, away)

                if label == "yes":  # the market itself is named after the combo
                    label = re.sub(r"\s+", " ", _mname(market)).strip()
                    for team, word in ((home.lower(), "home"), (away.lower(), "away")):
                        if len(team) > 2:
                            label = label.replace(team, word)

                for rule, pattern, win_based, text in _COMBO_RULES:
                    if win_based and tight:
                        continue  # tight games: no win-based picks

                    if not pattern.search(label):
                        continue

                    chance = min(0.95 / odd, 0.97)

                    if rule not in best or chance > best[rule]["p"]:
                        best[rule] = {
                            "kind": "combo",
                            "label": text.format(home=home, away=away),
                            "odd": odd,
                            "p": chance,
                            "market_p": chance,
                            "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                            "extra": True,
                            "combo": rule,
                        }

        found = sorted(best.values(), key=lambda c: -c["p"])
        return found[:COMBOS_PER_MATCH]

    def streak_candidates(event, markets):
        if NO_STREAK:
            return []  # "Any team to score 3+ goals in a row" is switched off

        found = []

        for market in markets or []:
            name = _mname(market)

            if "in a row" not in name or any(w in name for w in _HALF_WORDS):
                continue

            if not re.search(r"\b(3|three)\b", name):
                continue

            live = _live_outcomes(market)

            if len(live) < 2:
                continue

            total = sum(1 / odd for _, odd in live)
            spec = market.get("specifier") or ""

            for outcome, odd in live:
                answer = _olabel(outcome)

                if answer not in ("yes", "no"):
                    continue

                if not (STREAK_MIN_ODD <= odd <= STREAK_MAX_ODD):
                    continue

                chance = (1 / odd) / total

                found.append({
                    "kind": "streak",
                    "label": f"Any team to score 3+ goals in a row: {answer.capitalize()}",
                    "odd": odd,
                    "p": chance,
                    "market_p": chance,
                    "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                    "extra": True,
                })

        found.sort(key=lambda c: -c["p"])
        return found[:1]

    def _shots_side(name, home, away):
        h, a = home.lower(), away.lower()
        has_home = "home" in name or (len(h) > 3 and h in name)
        has_away = "away" in name or (len(a) > 3 and a in name)
        if has_home and has_away:
            return None
        if has_home:
            return "home"
        if has_away:
            return "away"
        return "total"

    def bounds_candidates(event, markets):
        """Goal bounds: total goals, Home team goals and Away team goals (one range each)."""
        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        best = {}

        for market in markets or []:
            name = _mname(market)

            if "bound" not in name or "corner" in name or any(w in name for w in _HALF_WORDS):
                continue

            side = _shots_side(name, home, away)

            if side is None:
                continue

            live = _live_outcomes(market)

            if len(live) < 2:
                continue

            total = sum(1 / odd for _, odd in live)
            spec = market.get("specifier") or ""

            for outcome, odd in live:
                rng = re.sub(r"\s+", "", _olabel(outcome)).replace("\u2013", "-").replace("\u2014", "-")

                if BOUNDS_WANTED and rng not in BOUNDS_WANTED:
                    continue

                if not (BOUNDS_MIN_ODD <= odd <= BOUNDS_MAX_ODD):
                    continue

                chance = min(0.95 / odd, 0.97)  # ranges overlap, so the price alone gives the chance

                if chance < BOUNDS_MIN_P:
                    continue

                who = {"total": "Total goals", "home": f"{home} goals", "away": f"{away} goals"}[side]

                item = {
                    "kind": "bounds",
                    "label": f"{who} {rng}",
                    "odd": odd,
                    "p": chance,
                    "market_p": chance,
                    "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                    "extra": True,
                }

                if side not in best or chance > best[side]["p"]:
                    best[side] = item

        found = sorted(best.values(), key=lambda c: -c["p"])
        return found[:BOUNDS_PER_MATCH]

    def halves_candidates(event, markets):
        """Both halves Over 1.5 (yes / no) and Both halves Under 1.5 (yes / no)."""
        found = []

        for market in markets or []:
            name = _mname(market)

            if "both halves" not in name or "corner" in name or "card" in name:
                continue

            live = _live_outcomes(market)

            if len(live) < 2:
                continue

            spec = market.get("specifier") or ""
            line = _num(spec.replace("total=", "")) if spec.startswith("total=") else None

            if line is None:
                found_line = re.search(r"(\d+\.5)", name)
                line = _num(found_line.group(1)) if found_line else None

            if line != 1.5:
                continue

            if "under" in name and "over" not in name:
                direction = "under"
            elif "over" in name and "under" not in name:
                direction = "over"
            else:
                continue

            total = sum(1 / odd for _, odd in live)

            for outcome, odd in live:
                answer = _olabel(outcome)

                if answer not in ("yes", "no"):
                    continue

                if not (HALVES_MIN_ODD <= odd <= HALVES_MAX_ODD):
                    continue

                chance = (1 / odd) / total

                found.append({
                    "kind": "halves",
                    "label": f"Both halves {direction} 1.5: {answer.capitalize()}",
                    "odd": odd,
                    "p": chance,
                    "market_p": chance,
                    "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                    "extra": True,
                })

        found.sort(key=lambda c: -c["p"])
        return found[:2]

    def fh_ft_candidates(event, markets):
        """1st half Over 0.5 together with full-time Over 1.5."""
        found = []

        for market in markets or []:
            name = _mname(market)

            if not ("1st half" in name or "first half" in name):
                continue

            if any(w in name for w in ("corner", "card", "booking", "1x2", "both", "odd", "handicap", "exact")):
                continue

            spec = market.get("specifier") or ""

            for outcome, odd in _live_outcomes(market):
                if not (FHFT_MIN_ODD <= odd <= FHFT_MAX_ODD):
                    continue

                label = _olabel(outcome).replace("\u2013", "-")

                if not re.search(r"over 0\.5.*(?:&|and).*over 1\.5", label):
                    continue

                chance = min(0.95 / odd, 0.97)

                found.append({
                    "kind": "fh_ft",
                    "label": "1st half Over 0.5 & Full-time Over 1.5",
                    "odd": odd,
                    "p": chance,
                    "market_p": chance,
                    "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                    "extra": True,
                })

        found.sort(key=lambda c: -c["p"])
        return found[:1]

    def draw10_candidates(event, markets):
        """The first 10 minutes end level (10 minutes 1X2, Draw)."""
        found = []

        for market in markets or []:
            name = _mname(market)

            if not re.search(r"\b10\s*(?:min|minutes)\b|first 10|0\s*-\s*10", name):
                continue

            if any(w in name for w in ("corner", "card", "total", "over", "under", "handicap")):
                continue

            live = _live_outcomes(market)

            if len(live) < 3:
                continue

            total = sum(1 / odd for _, odd in live)
            spec = market.get("specifier") or ""

            for outcome, odd in live:
                if _olabel(outcome) not in ("draw", "x"):
                    continue

                if not (DRAW10_MIN_ODD <= odd <= DRAW10_MAX_ODD):
                    continue

                chance = (1 / odd) / total

                found.append({
                    "kind": "draw10",
                    "label": "Draw after 10 minutes (10 min 1X2)",
                    "odd": odd,
                    "p": chance,
                    "market_p": chance,
                    "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                    "extra": True,
                })

        found.sort(key=lambda c: -c["p"])
        return found[:1]

    def either_half_candidates(event, markets, existing, tight):
        """Home team / Away team to win either half."""
        if tight:
            return []  # tight games: no win-based picks

        home = event.get("homeTeamName", "Home")
        away = event.get("awayTeamName", "Away")
        have = {c.get("label") for c in existing}
        best = {}

        for market in markets or []:
            name = _mname(market)

            if "either half" not in name or "both" in name or "corner" in name or "card" in name:
                continue

            spec = market.get("specifier") or ""

            for outcome, odd in _live_outcomes(market):
                if not (EITHER_HALF_MIN_ODD <= odd <= EITHER_HALF_MAX_ODD):
                    continue

                label = _norm_label(outcome, spec, home, away)

                if label in ("home", "1") or label.startswith("home"):
                    side = "home"
                elif label in ("away", "2") or label.startswith("away"):
                    side = "away"
                else:
                    continue

                text = f"{home if side == 'home' else away} to win either half"

                if text in have:
                    continue  # the normal ticket builder already offers this one

                chance = min(0.95 / odd, 0.97)

                if side not in best or chance > best[side]["p"]:
                    best[side] = {
                        "kind": "either_half",
                        "label": text,
                        "odd": odd,
                        "p": chance,
                        "market_p": chance,
                        "key": (str(market.get("id")), spec, str(outcome.get("id"))),
                        "side": side,
                        "extra": True,
                    }

        return list(best.values())

    def extra_candidates(event, markets, tight=False, existing=()):
        out = []

        for market in markets or []:
            _note_extra(market)

        for name, make in (
            ("combo", lambda: combo_candidates(event, markets, tight)),
            ("streak", lambda: streak_candidates(event, markets)),
            ("bounds", lambda: bounds_candidates(event, markets)),
            ("halves", lambda: halves_candidates(event, markets)),
            ("fh_ft", lambda: fh_ft_candidates(event, markets)),
            ("draw10", lambda: draw10_candidates(event, markets)),
            ("either_half", lambda: either_half_candidates(event, markets, existing, tight)),
        ):
            try:
                got = make()

                if got:
                    _EXTRA_COUNT[name] = _EXTRA_COUNT.get(name, 0) + len(got)

                out += got
            except Exception as exc:
                print(f"Extra markets ({name}) failed: {exc}")

        return out

    def _only_line_corners(candidates):
        """Corners are switched off: drop every corner pick (unless NO_CORNERS is False, then only Over 7.5)."""
        if NO_CORNERS:
            return [c for c in candidates if c.get("kind") != "corners"]

        return [
            c for c in candidates
            if c.get("kind") != "corners" or abs((c.get("line") or 0) - CORNERS_LINE) < 1e-9
        ]

    _orig_event_candidates = st.event_candidates

    def event_candidates(event, markets):
        if getattr(CTX, "corners", False):
            return corners_candidates(event, markets)

        base = _only_line_corners(_orig_event_candidates(event, markets))

        try:
            # Tight game: no win picks at all (corners are off, so only the extra picks are left).
            if not getattr(CTX, "straight", False) and _is_tight(markets):
                return corners_candidates(event, markets) + extra_candidates(event, markets, tight=True)

            base = base + team_goal_candidates(event, markets, base)
            base = base + second_half_candidates(event, markets, base)
            base = base + corners_candidates(event, markets)
            base = base + _double12(event, markets, DOUBLE12_SURE_DRAW, False)
            base = base + extra_candidates(event, markets, tight=False, existing=base)

            if getattr(CTX, "fallback", False) and not getattr(CTX, "straight", False):
                base = base + fallback_fillers(event, markets)

            return base
        except Exception:
            return base

    if ONLY_UP_AND_EITHER_HALF:
        _event_candidates_all = event_candidates

        def event_candidates(event, markets):
            got = _event_candidates_all(event, markets)
            return [c for c in got if c.get("kind") in ("up", "either_half")]

        # With only two kinds of picks, the ticket builder must not cap them.
        for _kind in ("up", "either_half"):
            st.KIND_CAP[_kind] = 1.0

    st.event_candidates = event_candidates

    if hasattr(st, "daily_candidates"):
        _orig_daily_candidates = st.daily_candidates

        def daily_candidates(event, markets):
            got = _only_line_corners(_orig_daily_candidates(event, markets))

            if ONLY_UP_AND_EITHER_HALF:
                got = [c for c in got if c.get("kind") in ("up", "either_half")]

            return got

        st.daily_candidates = daily_candidates

    # No direct win (only 1UP / 2UP), no BTTS, no Over 2 / 2.5 / 3.5 goals, no corners, no 3 goals in a row.
    _orig_allowed = st._candidate_is_allowed
    _NOT_WANTED_LABEL = re.compile(r"both teams|over (?:[2-9]|[1-9]\d)(?:\.\d)? goals", re.I)

    _ALLOWED_KINDS = {
        "up",  # 1UP / 2UP
        "either_half",  # win either half
        "team_goals",  # Home / Away to score 0.5
        "dc12",  # Home or Away (12)
        "over15",  # Over 1.5 goals
        "sh_goals",  # 2nd half Over 0.5 goals
        "combo",  # Home / Away or GG, Home / Away or Draw & Over 1.5
        "halves",  # both halves Over / Under 1.5 (yes / no)
        "fh_ft",  # 1st half Over 0.5 & full-time Over 1.5
        "draw10",  # draw after 10 minutes
        "bounds",  # goal bounds
    }
    if not NO_CORNERS:
        _ALLOWED_KINDS.add("corners")  # Over 7.5 corners and team corners
    if not NO_STREAK:
        _ALLOWED_KINDS.add("streak")  # Any team to score 3+ goals in a row

    _BAD_LABEL = re.compile(r"handicap|both teams|\bunder \d|\bcards?\b", re.I)

    if ALLOW_ALL_MARKETS:
        _ALLOWED_KINDS.update({"win", "btts", "over", "handicap", "dnb", "dc"})
        _BAD_LABEL = re.compile(r"\bunder \d|\bcards?\b", re.I)

    if ONLY_UP_AND_EITHER_HALF:
        _ALLOWED_KINDS = {"up", "either_half"}

    def _candidate_is_allowed(kind, label=""):
        k = str(kind or "").lower()
        low = str(label or "").lower()

        if k not in _ALLOWED_KINDS:
            return False  # no direct win, BTTS, handicap, Over 2 / 2.5, Under, double chance, corners, ...

        if k in EXTRA_KINDS:
            return not re.search(r"handicap|\bcards?\b|both teams", low)

        if _BAD_LABEL.search(low):
            return False

        if k == "team_goals" and "over 0.5" not in low:
            return False  # only "to score 0.5"

        return True

    st._candidate_is_allowed = _candidate_is_allowed

    _orig_passes_rules = st._passes_rules

    def _passes_rules(candidate, ctx, relax=0):
        if getattr(CTX, "corners", False):
            return True  # corners are chosen by their own prices, not the goal rules

        # Extra markets are chosen by their own prices too.
        if candidate.get("extra"):
            return True

        # Corner picks built from their own prices (not the goal rules).
        if candidate.get("kind") == "corners" and candidate.get("corner_kind"):
            return True

        # Tight game (for example home 2.06, away 3.23): no 1UP / 2UP win picks.
        if candidate.get("kind") == "up" and not getattr(CTX, "straight", False):
            ph, pa = ctx.get("ph"), ctx.get("pa")

            if ph is not None and pa is not None:
                if max(ph, pa) < TIGHT_MAX_P and min(ph, pa) >= TIGHT_MIN_P:
                    return False

        return _orig_passes_rules(candidate, ctx, relax)

    st._passes_rules = _passes_rules

    _orig_reason_for = st._reason_for

    def _reason_for(candidate):
        if candidate.get("kind") == "corners" and candidate.get("corner_kind"):
            chance = round(candidate.get("p", 0) * 100)
            line = candidate.get("line") or 0
            needed = int(line + 0.5)

            if candidate["corner_kind"] == "total":
                centre = candidate.get("corner_centre")
                if centre:
                    return (
                        f"SportyBet's corner prices centre near {centre:.1f} corners in this match, "
                        f"so Over {line:g} is about {chance}% likely."
                    )
                return f"SportyBet's corner prices give Over {line:g} corners about {chance}% chance."

            team = candidate["home"] if candidate["corner_kind"] == "home" else candidate["away"]
            return f"SportyBet's prices give {team} about {chance}% to win {needed} or more corners."

        if candidate.get("kind") == "sh_goals":
            chance = round(candidate.get("p", 0) * 100)
            return f"SportyBet's prices give at least one goal after half time about {chance}% chance."

        if candidate.get("kind") == "sh_12":
            chance = round(candidate.get("p", 0) * 100)
            return f"Either team wins the 2nd half in about {chance}% of cases: a drawn 2nd half looks unlikely."

        if candidate.get("kind") == "dc12":
            chance = round(candidate.get("p", 0) * 100)
            return f"Either team wins in about {chance}% of cases: a draw looks unlikely in this match."

        if candidate.get("kind") == "combo":
            chance = round(candidate.get("p", 0) * 100)
            return f"Two ways to win in one pick: SportyBet's prices give it about {chance}%."

        if candidate.get("kind") == "streak":
            chance = round(candidate.get("p", 0) * 100)
            if str(candidate.get("label", "")).lower().endswith("no"):
                return f"A run of 3 goals in a row is rare: SportyBet's prices give 'No' about {chance}%."
            return f"SportyBet's prices give this answer about {chance}%."

        if candidate.get("kind") == "bounds":
            chance = round(candidate.get("p", 0) * 100)
            return f"SportyBet's goal-range prices give this range about {chance}%."

        return _orig_reason_for(candidate)

    st._reason_for = _reason_for

    # ---- Entry points ----
    _orig_ticket_flow = bot.prediction_ticket_flow

    def prediction_ticket_flow(chat_id, text, *args, **kwargs):
        text = text or ""
        # Corners are switched off: a "corners" request never builds a corners ticket.
        wants_corners = (not NO_CORNERS) and bool(CORNERS_RE.search(text)) and not STRAIGHT_RE.search(text)
        cups = cups_from_text(text) if CUPS_RE.search(text) else None

        if wants_corners and not cups and not TOP_LEAGUES_RE.search(text):
            text = "top leagues " + text  # corners always come from the top leagues only

        CTX.football_note = None
        CTX.straight = bool(STRAIGHT_RE.search(text))
        st.MAX_DETAIL_EVENTS = max(MAX_DETAIL, STRAIGHT_DETAIL) if CTX.straight else MAX_DETAIL
        CTX.cups = cups  # European cups: only the chosen cups
        CTX.top_leagues = bool(TOP_LEAGUES_RE.search(text)) and not cups  # straight win uses ALL leagues
        CTX.corners = wants_corners

        # "send today's straight win and win either half"
        CTX.straight_either = CTX.straight and bool(EITHER_RE.search(text))
        # "send today's straight win 30 odds": today first, tomorrow only if the odds are not reached
        CTX.straight_extend = CTX.straight and bool(ODDS_RE.search(text))
        CTX.straight_today = CTX.straight and bool(TODAY_ONLY_RE.search(text))

        saved = None

        if CTX.corners:
            saved = (dict(st.KIND_CAP), st.MAX_PER_LEAGUE)
            st.KIND_CAP["corners"] = 10.0
            st.MAX_PER_LEAGUE = CORNERS_MAX_PER_LEAGUE
            CTX.corner_stats = {"events": 0, "with_team": 0, "names": set()}

        try:
            if (
                (CTX.top_leagues or CTX.cups)
                and not CTX.corners
                and not CTX.straight
                and LIST_RE.search(text)
                and not NUMBER_RE.search(text)
                and not ASKS_FOR_TICKET_RE.search(text)
            ):
                # "Top leagues games this week": just list every game.
                return top_leagues_list(chat_id, text)

            if (CTX.top_leagues or CTX.cups) and not CTX.straight:
                # Top leagues / cups tickets are built "safe", the same as when you ask in the chat.
                if not CTX.corners and not SAFE_WORD_RE.search(text):
                    text += " safe"

                # They asked for picks or odds: build a ticket from the top leagues / chosen cups.
                if not WINDOW_RE.search(text):
                    text += " this week"
                if not NUMBER_RE.search(text):
                    text += f" {CUPS_PICKS if CTX.cups else TOP_LEAGUE_PICKS} picks"

            return _orig_ticket_flow(chat_id, text, *args, **kwargs)
        finally:
            if saved is not None:
                st.KIND_CAP.clear()
                st.KIND_CAP.update(saved[0])
                st.MAX_PER_LEAGUE = saved[1]
                stats = getattr(CTX, "corner_stats", None) or {}
                print(
                    f"Corners: matches with corner prices={stats.get('events', 0)}, "
                    f"with team-corner prices={stats.get('with_team', 0)}. "
                    f"Corner markets seen: {sorted(stats.get('names', []))[:12]}"
                )

            st.MAX_DETAIL_EVENTS = MAX_DETAIL
            CTX.top_leagues = False
            CTX.straight = False
            CTX.straight_either = False
            CTX.straight_extend = False
            CTX.straight_today = False
            CTX.corners = False
            CTX.cups = None

    bot.prediction_ticket_flow = prediction_ticket_flow

    _orig_handle_text = bot.handle_text

    def handle_text(chat_id, text):
        message = str(text or "").strip()

        # The Straight win button goes straight to the ticket builder, so the AI
        # can never change what it asks for.
        if STRAIGHT_BUTTON_RE.match(message):
            return bot.prediction_ticket_flow(chat_id, "straight win")

        # Requests such as "send today's straight win 30 odds" or
        # "send today's straight win and win either half" also skip the AI.
        words = message.split()

        if (
            STRAIGHT_RE.search(message)
            and "?" not in message
            and not message.startswith("/")
            and not QUESTION_START_RE.match(message)
            and len(words) <= 12
            and (NUMBER_RE.search(message) or WINDOW_RE.search(message) or len(words) <= 5)
        ):
            return bot.prediction_ticket_flow(chat_id, message.lower())

        if (
            CUPS_RE.search(message)
            and "?" not in message
            and not message.startswith("/")
            and not QUESTION_START_RE.match(message)
            and len(words) <= 12
            and (NUMBER_RE.search(message) or WINDOW_RE.search(message) or len(words) <= 5)
        ):
            return bot.prediction_ticket_flow(chat_id, message.lower())

        return _orig_handle_text(chat_id, text)

    bot.handle_text = handle_text

    # ============================================================
    # CHAT: "are there any midnight games?" -> answer, then wait for "list" or "build"
    # ============================================================
    PENDING = {}  # chat_id -> (window words, time asked)
    PENDING_LOCK = threading.Lock()

    LOOK_RE = re.compile(r"\b(list|show|look|check|see|through|fixtures?|schedule)\b", re.I)
    LOOK_START_RE = re.compile(r"^(list|show|look|check|see|give me|display)\b", re.I)
    YES_RE = re.compile(
        r"^(yes|yeah|yea|yep|yup|ya|ok|okay|sure|please|go ahead|do it|go on|abeg|make it)\b",
        re.I,
    )

    def _kick(event):
        ms = event.get("estimateStartTime")
        if not ms:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    def _fmt(kickoff):
        return kickoff.astimezone(bot.LOCAL_TZ).strftime("%a %H:%M")

    def _games_for(text):
        """Returns (request, [(kickoff_utc, event), ...]) for the window named in text."""
        provider = getattr(bot, "SPORTYBET_PROVIDER", None)

        if provider is None:
            return None, []

        CTX.availability = True  # only checking what exists: no 1-hour buffer
        try:
            req = bot.parse_request(text)
        finally:
            CTX.availability = False

        rows = []

        for event in provider.get_upcoming(req["start"], req["end"]):
            kickoff = _kick(event)
            if kickoff and req["start"] <= kickoff <= req["end"]:
                rows.append((kickoff, event))

        rows.sort(key=lambda r: r[0])

        # Log what SportyBet really gave us, so a short list can be explained.
        try:
            loaded = provider._load_events()
            times = [k for k in (_kick(e) for e in loaded) if k]
            print(
                f"Games window '{req['label']}': {len(rows)} found. "
                f"SportyBet loaded {len(loaded)} matches in total, "
                f"from {_fmt(min(times))} to {_fmt(max(times))} ({bot.LOCAL_TZ_NAME})."
            )
        except Exception as exc:
            print(f"Games window log failed: {exc}")

        return req, rows

    def availability_reply(chat_id, message):
        low = message.lower()

        try:
            req, rows = _games_for(low)
        except Exception as exc:
            print(f"Availability check failed: {exc}")
            bot.send_message(chat_id, "\u274C I couldn't read SportyBet right now. Try again in a minute.")
            return

        if req is None:
            bot.send_message(chat_id, "\u274C SportyBet mode is off on this server.")
            return

        label = escape(str(req["label"]))

        if not rows:
            bot.send_message(chat_id, f"No, I can't see any games for {label} right now.")
            return

        n = len(rows)
        count = "is 1 game" if n == 1 else f"are {n} games"

        bot.send_message(
            chat_id,
            f"Yes, there {count} for {label}, "
            f"from <b>{_fmt(rows[0][0])}</b> to <b>{_fmt(rows[-1][0])}</b> ({bot.LOCAL_TZ_NAME}).\n\n"
            "Say <b>list</b> and I'll show them all, or say <b>yes</b> / <b>build</b> "
            "and I'll build a safe ticket from them (you can add odds, like \"build 2 odds\").",
        )

        with PENDING_LOCK:
            PENDING[chat_id] = (_window_phrase(low) or "today", time.time())

    def list_all_games(chat_id, text):
        try:
            req, rows = _games_for(text.lower())
        except Exception as exc:
            print(f"Game list failed: {exc}")
            bot.send_message(chat_id, "\u274C I couldn't read SportyBet right now. Try again in a minute.")
            return

        if req is None:
            bot.send_message(chat_id, "\u274C SportyBet mode is off on this server.")
            return

        if not rows:
            bot.send_message(chat_id, f"I can't see any games for {escape(str(req['label']))}.")
            return

        total = len(rows)

        if total > LIST_MAX:
            rows = sorted(rows, key=lambda r: (_top_rank(r[1]), r[0]))[:LIST_MAX]
            rows.sort(key=lambda r: r[0])

        lines = [
            f"\u26BD <b>GAMES - {escape(str(req['label']).upper())}</b>",
            f"\U0001F4CA {total} match" + ("es" if total != 1 else "") + f" - times in {bot.LOCAL_TZ_NAME}",
        ]

        if total > LIST_MAX:
            lines.append(f"(showing {LIST_MAX}, top leagues first)")

        current_day = None

        for number, (kickoff, event) in enumerate(rows, 1):
            local = kickoff.astimezone(bot.LOCAL_TZ)

            if local.date() != current_day:
                current_day = local.date()
                lines += ["", f"\U0001F4C6 <b>{escape(local.strftime('%A, %d %b'))}</b>"]

            category = ((event.get("sport") or {}).get("category") or {})
            league = str((category.get("tournament") or {}).get("name") or "")

            lines.append(
                f"{number}. {local.strftime('%H:%M')} - "
                f"{escape(str(event.get('homeTeamName') or 'Home'))} vs "
                f"{escape(str(event.get('awayTeamName') or 'Away'))} <i>({escape(league)})</i>"
            )

        bot.send_message(chat_id, "\n".join(lines))

    def _build_from_pending(chat_id, phrase, low):
        request = f"{phrase} {low}"

        if not SAFE_WORD_RE.search(request):
            request += " safe"
        if not NUMBER_RE.search(request):
            request += " 5 picks"

        return bot.prediction_ticket_flow(chat_id, request)

    _prev_handle_text = bot.handle_text

    def handle_text(chat_id, text):
        message = str(text or "").strip()
        low = message.lower()
        words = low.split()

        if message and not message.startswith("/"):

            # 1) Answer to "list or build?"  (the question stays open for 15 minutes)
            with PENDING_LOCK:
                pend = PENDING.get(chat_id)

                if pend and time.time() - pend[1] > PENDING_SECONDS:
                    PENDING.pop(chat_id, None)
                    pend = None

            if pend and len(words) <= 10 and not _has_window(low):
                phrase = pend[0]

                if BUILD_REPLY_RE.search(low):  # "build", "build 2 odds from it", "safe", "ticket"
                    return _build_from_pending(chat_id, phrase, low)

                if LIST_REPLY_RE.search(low):  # "list", "show", "full list", "all"
                    with PENDING_LOCK:
                        PENDING.pop(chat_id, None)

                    return list_all_games(chat_id, phrase)

                if YES_RE.match(low):  # "yes", "yeah", "ok", "go ahead" = build it
                    return _build_from_pending(chat_id, phrase, "")

            plain = not (
                TOP_LEAGUES_RE.search(low)
                or CUPS_RE.search(low)
                or CORNERS_RE.search(low)
                or STRAIGHT_RE.search(low)
            )

            # 2) "Are there any midnight games?"
            if (
                plain and _has_window(low) and GAMES_WORD_RE.search(low)
                and AVAIL_ASK_RE.search(low) and not LOOK_START_RE.match(low)
                and len(words) <= 12
                and not BUILD_WORDS_RE.search(low) and not NUMBER_RE.search(low)
            ):
                return availability_reply(chat_id, message)

            # 3) "show tomorrow morning games" / "look through tomorrow games"
            if (
                plain and _has_window(low) and GAMES_WORD_RE.search(low)
                and LOOK_RE.search(low) and len(words) <= 10
                and not BUILD_WORDS_RE.search(low) and not NUMBER_RE.search(low)
            ):
                return list_all_games(chat_id, low)

            # 4) "build a safe 5 odds tomorrow morning" goes straight to the ticket builder
            if (
                _has_window(low) and BUILD_WORDS_RE.search(low)
                and "?" not in message and not QUESTION_START_RE.match(low)
                and len(words) <= 14
            ):
                request = low

                if not NUMBER_RE.search(request):
                    request += " 5 picks"

                return bot.prediction_ticket_flow(chat_id, request)

        return _prev_handle_text(chat_id, text)

    bot.handle_text = handle_text

    # ============================================================
    # NO U19 / U21 (youth) GAMES + DIFFERENT MATCHES IN EVERY TICKET
    # (this block must appear ONLY ONCE, at the very end of the file,
    # inside "if smart_ticket is not None:")
    # ============================================================

    # ---- 1) No youth games (U15 to U23). Women's leagues stay allowed. ----
    BLOCK_YOUTH = True  # set to False to allow youth games again

    _YOUTH_PART = r"\bu[-\s]?(?:1[5-9]|2[0-3])\b|youth"
    _YOUTH_RE = re.compile(_YOUTH_PART, re.I)

    if BLOCK_YOUTH:
        st.SKIP_LEAGUE_RE = re.compile(r"friendl|" + _YOUTH_PART, re.I)
        st._UNRELIABLE_RE = re.compile(r"friendl|reserve|academy|development|" + _YOUTH_PART, re.I)

    def _is_youth(event):
        category = ((event.get("sport") or {}).get("category") or {})
        league = str((category.get("tournament") or {}).get("name") or "")
        text = f"{league} {event.get('homeTeamName') or ''} {event.get('awayTeamName') or ''}"
        return bool(_YOUTH_RE.search(text))

    _games_for_with_youth = _games_for

    def _games_for(text):
        # the "are there any ... games?" answers and lists skip youth games too
        req, rows = _games_for_with_youth(text)

        if BLOCK_YOUTH:
            rows = [r for r in rows if not _is_youth(r[1])]

        return req, rows

    # ---- 2) Different matches in every ticket (never starves a big ticket) ----
    AVOID_REPEAT = True   # False = tickets may reuse matches again
    USED_HOURS = 1        # a match used in a ticket counts as "used" for this many hours
    MIN_FRESH = 30        # if fewer unused matches than this, the oldest used ones are added back

    _USED = {}  # chat_id -> {event_id: time used}
    _USED_LOCK = threading.Lock()
    _ACTIVE = {"chat": None}

    def _used_ids(chat_id):
        cutoff = time.time() - USED_HOURS * 3600

        with _USED_LOCK:
            book = _USED.get(chat_id, {})

            for key in [k for k, t in book.items() if t < cutoff]:
                book.pop(key, None)

            return set(book)

    def _remember(chat_id, ids):
        with _USED_LOCK:
            book = _USED.setdefault(chat_id, {})
            now = time.time()

            for event_id in ids:
                if event_id is not None:
                    book[event_id] = now

    _gather_before_repeat = st.gather

    def gather(*args, **kwargs):
        result = _gather_before_repeat(*args, **kwargs)

        if not AVOID_REPEAT or kwargs.get("daily") or getattr(CTX, "is_daily", False):
            return result

        if getattr(CTX, "straight", False):
            return result  # straight win is never filtered

        chat_id = getattr(CTX, "chat_id", None) or _ACTIVE["chat"]

        if chat_id is None:
            return result

        try:
            used = _used_ids(chat_id)

            if not used:
                return result

            with _USED_LOCK:
                book = dict(_USED.get(chat_id, {}))

            fresh = [g for g in result[0] if g and g[0].get("event_id") not in used]

            if len(fresh) < MIN_FRESH:
                old = sorted(
                    (g for g in result[0] if g and g[0].get("event_id") in used),
                    key=lambda g: book.get(g[0].get("event_id"), 0),
                )
                fresh += old[:MIN_FRESH - len(fresh)]

            print(f"Different matches: {len(fresh)} matches available for this ticket")
            return (fresh,) + tuple(result[1:])
        except Exception as exc:
            print(f"Different-matches filter failed: {exc}")
            return result

    st.gather = gather

    _build_before_repeat = st.build_ticket

    def build_ticket(*args, **kwargs):
        built = _build_before_repeat(*args, **kwargs)

        try:
            CTX.last_ids = [c.get("event_id") for c in (built.get("chosen") or [])]
        except Exception:
            pass

        return built

    st.build_ticket = build_ticket

    _flow_before_repeat = bot.prediction_ticket_flow

    def prediction_ticket_flow(chat_id, text, *args, **kwargs):
        CTX.chat_id = chat_id
        _ACTIVE["chat"] = chat_id
        CTX.last_ids = []
        CTX.is_daily = bool(st.DAILY_RE.search(str(text or "").lower()))

        try:
            return _flow_before_repeat(chat_id, text, *args, **kwargs)
        finally:
            try:
                if (
                    AVOID_REPEAT
                    and not CTX.is_daily
                    and CTX.last_ids
                    and not STRAIGHT_RE.search(str(text or ""))
                ):
                    _remember(chat_id, CTX.last_ids)
            except Exception as exc:
                print(f"Could not remember used matches: {exc}")

            CTX.chat_id = None
            CTX.is_daily = False
            _ACTIVE["chat"] = None

    bot.prediction_ticket_flow = prediction_ticket_flow

    # Say "reset tickets" to let every match be used again.
    RESET_RE = re.compile(r"^(reset|clear|forget)\b.*\b(tickets?|matches|used|picks)\b", re.I)

    _text_before_repeat = bot.handle_text

    def handle_text(chat_id, text):
        if RESET_RE.match(str(text or "").strip()):
            with _USED_LOCK:
                _USED.pop(chat_id, None)

            return bot.send_message(
                chat_id,
                "Done. I cleared the matches from your earlier tickets, so any match can be used again.",
            )

        return _text_before_repeat(chat_id, text)

    bot.handle_text = handle_text
