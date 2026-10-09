"""Upgrades for SamuelBet AI. Does NOT edit main.py.

1) Straight wins use SportyBet's "1X2 - 1UP" or "1X2 - 2UP" market:
   - bot is quite sure (chance >= UP_2UP_MIN_PROB)  -> 2UP (better odds)
   - bot is less sure                               -> 1UP (safer, lower odds)
   - SportyBet does not offer it for that match     -> normal Home/Away pick
2) The AI chat talks like a real person.
3) TOP LEAGUES: the card opens a window that asks for the odds, then builds a
   predicted ticket from the 17 top competitions (league games Fri to Sun, European
   cup games in midweek). "Top leagues games this week" lists every game instead.
4) STRAIGHT WIN BUTTON: only 1UP and 2UP picks. Team chance 55% to 59% -> 1UP,
   60% and above -> 2UP. Every pick pays at least 1.30. Soonest games first,
   searching up to a week ahead.
5) "weekend" now means Friday to Sunday.
6) Under every ticket the site says whether real football stats were used.

Put this file next to main.py and import it in app.py AFTER the launcher.
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from html import escape

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
- It can look at up to 7 days. A 100 odds ticket needs around 15 to 25 picks, so tell the user honestly
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
# SETTINGS YOU CAN CHANGE
# ------------------------------------------------------------
SEARCH_DAYS = 7             # how far ahead the bot may look (days)
EVENT_PAGES = 20            # how many pages of SportyBet matches to read (100 matches per page)

# Memory safety (Render free plan has 512 MB)
MAX_DETAIL = 70             # how many matches get their full list of markets read
DETAIL_WORKERS = 6          # how many are read at the same time
CACHE_LIMIT = 120           # how many matches are remembered in memory

# Straight win button (1UP / 2UP only)
STRAIGHT_LEGS = 10          # how many games to aim for
STRAIGHT_MIN_ODD = 1.30     # every pick must pay at least this much (no tiny odds)
STRAIGHT_MIN_P = 0.30       # the pick itself must have at least this chance after the checks
STRAIGHT_DETAIL = 90        # for Straight win: how many matches get their 1UP / 2UP prices read
STRAIGHT_MAX_STRENGTH = 0.85  # teams stronger than this pay too little, so they are not read
SURE_HOME_P = 0.55          # a home team needs at least 55% chance to win
SURE_AWAY_P = 0.55          # an away team needs at least 55% chance to win
UP2_MIN_P = 0.60            # 55% to 59%  -> 1UP.   60% and above -> 2UP (bigger odds)
SOONER_BONUS = 0.012        # small push towards games that kick off sooner

# Top leagues button
TOP_LEAGUE_PICKS = 8        # picks on the ticket when the person does not say a number


# ------------------------------------------------------------
# 4) TOP LEAGUES (17 competitions)
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


# ------------------------------------------------------------
# 5) Windows, straight win, weekend, football note
# ------------------------------------------------------------
WINDOW_RE = re.compile(r"\b(today|tonight|tomorrow|weekend|weekdays?|midweek|week|\d+\s*days?)\b", re.I)
WEEKDAYS_RE = re.compile(r"\b(weekdays?|midweek)\b", re.I)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:odds?|picks?|games?|matches|selections?|tips?|legs?)\b", re.I)
STRAIGHT_RE = re.compile(r"\bstraight\s*-?\s*(?:win|winning|wins)\b", re.I)
WEEKEND_RE = re.compile(r"\bweekend\b", re.I)
DAYS_RE = re.compile(r"\d+\s*days?\b", re.I)
STRAIGHT_BUTTON_RE = re.compile(r"^straight\s*-?\s*wins?(\s+tickets?)?$", re.I)


if smart_ticket is not None:
    import sportybet_provider as sp

    st = smart_ticket

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
            try:
                return _orig_study(*args, **kwargs)
            finally:
                _trim(getattr(smart_ticket, "_study_cache", {}), CACHE_LIMIT)

        smart_ticket.study = study

    smart_ticket.MAX_DETAIL_EVENTS = MAX_DETAIL
    smart_ticket.DETAIL_WORKERS = DETAIL_WORKERS

    # ---- Top leagues: keep only the 17 competitions (before the matches are studied) ----
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

    # ---- Windows: weekend = Friday to Sunday, straight win = up to a week ----
    _orig_parse_request = bot.parse_request

    def parse_request(text):
        req = _orig_parse_request(text)
        low = str(text or "").lower()
        now = datetime.now(timezone.utc)

        if WEEKDAYS_RE.search(low) and not DAYS_RE.search(low):
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
            weekday = local_now.weekday()          # Monday = 0 ... Sunday = 6
            earliest = now + timedelta(hours=getattr(bot, "MIN_HOURS_BEFORE_KICKOFF", 1))

            if weekday <= 4:                       # Monday to Friday: next Friday to Sunday
                friday = midnight + timedelta(days=4 - weekday)
                start, end = max(earliest, friday), friday + timedelta(days=3)
            elif weekday == 5:                     # Saturday: Saturday and Sunday
                start, end = max(earliest, midnight), midnight + timedelta(days=2)
            else:                                  # Sunday: today only
                start, end = max(earliest, midnight), midnight + timedelta(days=1)

            req["start"], req["end"] = start, end
            req["label"] = "this weekend (Fri to Sun)"

        elif STRAIGHT_RE.search(low) and not WINDOW_RE.search(low):
            req["end"] = req["start"] + timedelta(days=SEARCH_DAYS)
            req["label"] = f"next {SEARCH_DAYS} days"

        return req

    bot.parse_request = parse_request

    # ---- Straight win: ONLY 1UP / 2UP ----
    # Team chance to win (from the market, margin removed):
    #   55% to 59%  -> 1UP  (safer: pays as soon as the team leads by 1)
    #   60% or more -> 2UP  (bigger odds), or 1UP if 2UP pays too little
    # Every pick must pay at least STRAIGHT_MIN_ODD, so no tiny odds.
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

        for side, need in (("home", SURE_HOME_P), ("away", SURE_AWAY_P)):
            strength = chances[side]

            if strength < need:
                continue

            mine = [c for c in ups if c.get("side") == side]
            up1 = next((c for c in mine if c.get("up_level") == 1), None)
            up2 = next((c for c in mine if c.get("up_level") == 2), None)

            if strength >= UP2_MIN_P:
                order = [up2, up1]
            else:
                order = [up1]

            pick = next((c for c in order if c and c.get("odd", 0) >= STRAIGHT_MIN_ODD), None)

            if pick:
                pick = dict(pick)
                pick["market_side_p"] = strength
                pick["side"] = side
                out.append(pick)

        return out

    def choose_straight(groups, target, count):
        now = datetime.now(timezone.utc)

        best = []

        for group in groups:
            usable = [c for c in group if c.get("p", 0) > 0]

            if usable:
                best.append(max(usable, key=lambda c: c.get("p", 0)))

        def score(candidate):
            days = max(0.0, (candidate["kickoff"] - now).total_seconds() / 86400)
            return candidate.get("p", 0) - SOONER_BONUS * days

        best.sort(key=score, reverse=True)

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

    # ---- Remember how many matches really got football stats (shown under the ticket) ----
    _orig_build_ticket = st.build_ticket

    def build_ticket(*args, **kwargs):
        built = _orig_build_ticket(*args, **kwargs)
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

    # ---- Top leagues: list every game of the 17 competitions ----
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
                "\u26BD <b>TOP LEAGUES</b>\n\n"
                f"I couldn't find top league games for {escape(req['label'])}. "
                "Try again a bit later.",
            )
            return

        rows.sort(key=lambda r: (r[0], r[1], r[3]))

        lines = [
            "\u26BD <b>TOP LEAGUES</b>",
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
        if not getattr(CTX, "straight", False):
            return _orig_detail_order(events)

        band = []

        for event in events:
            try:
                strength = st.favourite_strength(event)
            except Exception:
                continue

            if min(SURE_HOME_P, SURE_AWAY_P) <= strength <= STRAIGHT_MAX_STRENGTH:
                band.append((strength, event))

        band.sort(key=lambda item: -item[0])

        if len(band) <= STRAIGHT_DETAIL:
            return [event for _, event in band]

        # Spread evenly over the whole band so every strength level is covered.
        step = len(band) / STRAIGHT_DETAIL
        return [band[int(i * step)][1] for i in range(STRAIGHT_DETAIL)]

    st._detail_order = _detail_order

    _orig_gather = st.gather

    def gather(*args, **kwargs):
        result = _orig_gather(*args, **kwargs)

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

    # ---- Entry points ----
    _orig_ticket_flow = bot.prediction_ticket_flow

    def prediction_ticket_flow(chat_id, text, *args, **kwargs):
        text = text or ""
        CTX.football_note = None
        CTX.top_leagues = bool(TOP_LEAGUES_RE.search(text))
        CTX.straight = bool(STRAIGHT_RE.search(text))

        try:
            if (
                CTX.top_leagues
                and LIST_RE.search(text)
                and not NUMBER_RE.search(text)
                and not ASKS_FOR_TICKET_RE.search(text)
            ):
                # "Top leagues games this week": just list every game.
                return top_leagues_list(chat_id, text)

            if CTX.top_leagues:
                # They asked for picks or odds: build a ticket from the top leagues.
                if not WINDOW_RE.search(text):
                    text += " this week"
                if not NUMBER_RE.search(text):
                    text += f" {TOP_LEAGUE_PICKS} picks"

            return _orig_ticket_flow(chat_id, text, *args, **kwargs)
        finally:
            CTX.top_leagues = False
            CTX.straight = False

    bot.prediction_ticket_flow = prediction_ticket_flow

    _orig_handle_text = bot.handle_text

    def handle_text(chat_id, text):
        message = str(text or "").strip()

        # The Straight win button goes straight to the ticket builder, so the AI
        # can never change what it asks for.
        if STRAIGHT_BUTTON_RE.match(message):
            return bot.prediction_ticket_flow(chat_id, "straight win")

        # Short requests such as "straight win 20 odds tomorrow" also skip the AI.
        if (
            STRAIGHT_RE.search(message)
            and (NUMBER_RE.search(message) or WINDOW_RE.search(message))
            and "?" not in message
            and len(message.split()) <= 8
            and not message.startswith("/")
        ):
            return bot.prediction_ticket_flow(chat_id, message.lower())

        return _orig_handle_text(chat_id, text)

    bot.handle_text = handle_text
