"""Chat flow for basketball: ask first, then build.

    you:  are there ncaa tonight
    bot:  Yes, there are 28 NCAA games for tonight (from Sun 20:00 to Mon 04:30 WAT).
          Tell me the odds you want (for example "100 odds") and I'll build it. Say "list" to see them.
    you:  100 odds
    bot:  (builds the ticket)
          If the ticket cannot reach 100 odds, the bot says how far it got and asks:
          "Want me to add tomorrow's games too?"   you: yes   -> it builds again from tonight + tomorrow.

Load it AFTER upgrades.py, ticket_extras.py and ticket_sub_fix.py (app.py block):

    try:
        import ticket_extras
        import ticket_sub_fix
        import ticket_ask
    except Exception as exc:
        print(f"NOTE: ticket extras are off: {type(exc).__name__}: {exc}")

Works for: ncaa, college basketball, nba, euroleague, basketball. Football and the old
"are there any midnight games?" flow are not touched.

The games are counted from the same SportyBet basketball list, with the same league check,
that the ticket builder uses (smart_ticket._bb_events), so the count matches what it can build from.
"""

import re
import threading
import time
from datetime import datetime, timezone
from html import escape

import main as bot
import smart_ticket as st
import sportybet_provider as sp
import upgrades as up

PENDING_SECONDS = 15 * 60  # how long the bot waits for your answer
LIST_MAX = 60

SPORT_RE = re.compile(r"\b(ncaab?|college basketball|euro\s*league|nba|basketball)\b", re.I)
AVAIL_RE = re.compile(r"^(are|is|any|do|does|got|have|how many|what|which)\b|\bany\b|\?\s*$", re.I)
BUILD_RE = re.compile(r"\b(build|ticket|make|give|odds?|picks?|safe|acca|accumulator|code)\b", re.I)
ODDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*odds?\b", re.I)
LIST_RE = re.compile(r"\b(list|show|fixtures?|all)\b", re.I)
GO_RE = re.compile(r"^(build|go|make|ok|okay|yes|yeah|yea|yep|sure|please|do it)\b", re.I)
NO_RE = re.compile(r"^(no|nope|nah|stop|cancel|leave it|never ?mind)\b", re.I)
TOTAL_RE = re.compile(r"Total odds:\s*(?:<[^>]+>)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
WINDOW_RE = re.compile(r"\b(today|tonight|tomorrow|morning|afternoon|evening|night|midnight|weekend|week|\d+\s*days?)\b", re.I)

SHORT_OK = 0.95  # a ticket with at least this share of the wanted odds counts as reached

_PENDING = {}  # chat_id -> state
_LOCK = threading.Lock()


# ------------------------------------------------------------
# which games are basketball / NCAA (the same source and the same league check the ticket builder uses)
# ------------------------------------------------------------
def _label(word):
    w = word.lower()
    if w.startswith("ncaa"):
        return "NCAA"
    if w == "nba":
        return "NBA"
    if "euro" in w:
        return "Euroleague"
    if "college" in w:
        return "college basketball"
    return "basketball"


def _league_text(event):
    """League and country name, as the ticket builder reads them."""
    try:
        league = (sp.sporty_fixture(event).get("league") or {})
        return f"{league.get('name', '')} {league.get('country', '')}"
    except Exception:
        sport = event.get("sport") or {}
        category = sport.get("category") or {}
        tournament = category.get("tournament") or {}
        return f"{category.get('name', '')} {tournament.get('name', '')}"


def _games(phrase, word):
    """(request, [(kickoff, event), ...], how many basketball games in all) for the window in phrase."""
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)

    if provider is None:
        return None, [], 0

    up.CTX.availability = True  # only checking what exists: no 1-hour buffer
    try:
        req = bot.parse_request(phrase)
    finally:
        up.CTX.availability = False

    _sport, league = st._parse_sport(word)
    pattern = st.BB_LEAGUES.get(league) if league else None

    rows = []
    total = 0

    for event in st._bb_events(provider):
        ms = event.get("estimateStartTime")

        if not ms:
            continue

        kickoff = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

        if not (req["start"] <= kickoff <= req["end"]):
            continue

        total += 1

        if pattern and not pattern.search(_league_text(event)):
            continue

        rows.append((kickoff, event))

    rows.sort(key=lambda r: r[0])

    print(f"Ask: '{req['label']}': {total} basketball games, {len(rows)} for {word!r}")

    return req, rows, total


def _set(chat_id, **state):
    state["t"] = time.time()
    with _LOCK:
        _PENDING[chat_id] = state


def _get(chat_id):
    with _LOCK:
        state = _PENDING.get(chat_id)
        if state and time.time() - state["t"] > PENDING_SECONDS:
            _PENDING.pop(chat_id, None)
            return None
        return state


def _clear(chat_id):
    with _LOCK:
        _PENDING.pop(chat_id, None)


# ------------------------------------------------------------
# 1) "are there ncaa tonight" -> count first
# ------------------------------------------------------------
def _availability(chat_id, low):
    word = SPORT_RE.search(low).group(1)
    phrase = up._window_phrase(low) or "today"

    try:
        req, rows, total = _games(phrase, word)
    except Exception as exc:
        print(f"Ask: availability failed: {type(exc).__name__}: {exc}")
        return bot.send_message(chat_id, "\u274C I couldn't read SportyBet right now. Try again in a minute.")

    if req is None:
        return bot.send_message(chat_id, "\u274C SportyBet mode is off on this server.")

    name = _label(word)
    when = escape(str(req["label"]))

    if not rows:
        extra = f" (SportyBet has {total} other basketball games in that time)" if total else ""
        return bot.send_message(
            chat_id,
            f"No, I can't see any {name} games for {when} right now{extra}. "
            "Want me to check tomorrow? Just ask \"are there " + word + " tomorrow\".",
        )

    n = len(rows)
    count = f"is <b>1</b> {name} game" if n == 1 else f"are <b>{n}</b> {name} games"

    _set(chat_id, stage="odds", word=word, phrase=phrase, n=n, name=name)

    return bot.send_message(
        chat_id,
        f"Yes, there {count} for {when}, from <b>{up._fmt(rows[0][0])}</b> to <b>{up._fmt(rows[-1][0])}</b> "
        f"({bot.LOCAL_TZ_NAME}).\n\n"
        "Tell me the odds you want (for example <b>100 odds</b>) and I'll build it. "
        "Say <b>list</b> to see the games.",
    )


def _list(chat_id, state):
    try:
        req, rows, _total = _games(state["phrase"], state["word"])
    except Exception as exc:
        print(f"Ask: list failed: {type(exc).__name__}: {exc}")
        return bot.send_message(chat_id, "\u274C I couldn't read SportyBet right now. Try again in a minute.")

    if not rows:
        return bot.send_message(chat_id, f"I can't see any {state['name']} games for that time now.")

    lines = [f"\U0001F3C0 <b>{escape(state['name'].upper())} - {escape(str(req['label']).upper())}</b>",
             f"\U0001F4CA {len(rows)} games - times in {bot.LOCAL_TZ_NAME}"]
    day = None

    for number, (kickoff, event) in enumerate(rows[:LIST_MAX], 1):
        local = kickoff.astimezone(bot.LOCAL_TZ)

        if local.date() != day:
            day = local.date()
            lines += ["", f"\U0001F4C6 <b>{escape(local.strftime('%A, %d %b'))}</b>"]

        lines.append(
            f"{number}. {local.strftime('%H:%M')} - {escape(str(event.get('homeTeamName') or 'Home'))} vs "
            f"{escape(str(event.get('awayTeamName') or 'Away'))}"
        )

    if len(rows) > LIST_MAX:
        lines.append(f"(showing the first {LIST_MAX})")

    lines += ["", "Now tell me the odds you want, for example <b>100 odds</b>."]
    bot.send_message(chat_id, "\n".join(lines))


# ------------------------------------------------------------
# 2) build, and ask to add tomorrow if the odds are not reached
# ------------------------------------------------------------
def _run(chat_id, request):
    """Runs the normal ticket builder and returns the odds of the ticket it sent (or None)."""
    sent = []
    original = bot.send_message

    def capture(cid, text, *args, **kwargs):
        sent.append(str(text))
        return original(cid, text, *args, **kwargs)

    bot.send_message = capture  # safe: app.py runs one chat turn at a time

    try:
        bot.prediction_ticket_flow(chat_id, request)
    finally:
        bot.send_message = original

    for text in reversed(sent):
        match = TOTAL_RE.search(text)
        if match:
            return float(match.group(1))

    return None


def _build(chat_id, state, message, target):
    word, phrase = state["word"], state["phrase"]
    request = f"{word} {phrase} {message}".strip()

    print(f"Ask: building {request!r} (wanted {target:g} odds)")

    reached = _run(chat_id, request)

    if reached is not None and reached >= target * SHORT_OK:
        return _clear(chat_id)

    got = f"only reach <b>{reached:g}</b> odds" if reached else "not build a ticket"

    if state.get("extended"):
        _clear(chat_id)
        return bot.send_message(
            chat_id,
            f"Even with tomorrow's games I could {got} out of the {target:g} you asked for. "
            "Try fewer odds, or ask for a longer window (for example \"" + word + " this week " + f"{target:g} odds\").",
        )

    _set(chat_id, stage="extend", word=word, phrase=phrase, target=target, name=state["name"], n=state.get("n"))

    return bot.send_message(
        chat_id,
        f"I could {got} out of the {target:g} you asked for from the {state.get('n') or ''} {state['name']} games "
        f"for {phrase}.\n\nWant me to add tomorrow's games too? Say <b>yes</b>, or tell me different odds.",
    )


def _extend(chat_id, state):
    target = state["target"]
    word = state["word"]

    _set(chat_id, stage="odds", word=word, phrase="2 days", name=state["name"], n=None, extended=True)

    # "2 days" = from now, covering tonight and all of tomorrow
    return _build(chat_id, _get(chat_id), f"{target:g} odds", target)


def _follow_up(chat_id, state, message, low, words):
    """Returns True when the message was an answer to the open question."""

    if len(words) > 10 or WINDOW_RE.search(low) or SPORT_RE.search(low):
        return False  # a new request: let the normal flow read it

    if state["stage"] == "extend":
        if GO_RE.match(low):
            _extend(chat_id, state)
            return True

        if NO_RE.match(low):
            _clear(chat_id)
            bot.send_message(chat_id, "Okay, I'll leave it there.")
            return True

    odds = ODDS_RE.search(low)

    if odds:
        _build(chat_id, dict(state, extended=False), message, float(odds.group(1)))
        return True

    if state["stage"] == "odds":
        if LIST_RE.search(low):
            _list(chat_id, state)
            return True

        if GO_RE.match(low):
            bot.send_message(chat_id, "How many odds do you want? For example <b>100 odds</b>.")
            return True

    return False


# ------------------------------------------------------------
# hook into the chat
# ------------------------------------------------------------
_prev_handle_text = bot.handle_text


def handle_text(chat_id, text):
    message = str(text or "").strip()
    low = message.lower()
    words = low.split()

    is_question = bool(
        message and not message.startswith("/")
        and SPORT_RE.search(low) and len(words) <= 12 and AVAIL_RE.search(low)
        and not BUILD_RE.search(low) and not ODDS_RE.search(low)
    )

    try:
        if message and not message.startswith("/"):
            state = _get(chat_id)

            if state and _follow_up(chat_id, state, message, low, words):
                return

            if is_question:
                return _availability(chat_id, low)
    except Exception as exc:
        print(f"Ask: failed: {type(exc).__name__}: {exc}")

        if is_question:
            # a question must never turn into a ticket
            return bot.send_message(chat_id, "\u274C I couldn't check the games right now. Try again in a minute.")

    return _prev_handle_text(chat_id, text)


bot.handle_text = handle_text

# ------------------------------------------------------------
# basketball tickets: points only (no handicap, no "to win"), sure picks only
# ------------------------------------------------------------
# Kept in this file on purpose, so one file switches everything on.
import os


def _on(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


BB_SURE_P = float(os.getenv("BB_SURE_P", "0.62"))  # 0.65 = safer, 0.58 = more games
_BB_DROP_KINDS = set()

if _on("BB_NO_HCP", "1"):
    _BB_DROP_KINDS.add("bb_hcp")

if _on("BB_NO_WINS", "1"):
    _BB_DROP_KINDS.add("bb_win")

try:
    _prev_bb = st.basketball_candidates

    def basketball_candidates(event, markets):
        return [
            c
            for c in _prev_bb(event, markets)
            if c.get("kind") not in _BB_DROP_KINDS and c.get("p", 0) >= BB_SURE_P
        ]

    st.basketball_candidates = basketball_candidates

    extra = ("|handicap" if "bb_hcp" in _BB_DROP_KINDS else "") + (r"|\bto win\b" if "bb_win" in _BB_DROP_KINDS else "")
    st._BB_LABEL_BAD = re.compile(st._BB_LABEL_BAD.pattern + extra, st._BB_LABEL_BAD.flags)

    # ticket_extras lowers the bar to 52% for basketball: keep it at BB_SURE_P instead
    try:
        import ticket_extras as _te
        if hasattr(_te, "BB_MIN_P"):
            _te.BB_MIN_P = max(float(_te.BB_MIN_P), BB_SURE_P)
    except Exception:
        pass

    print(f"OK: basketball filter on (points only, picks from {round(BB_SURE_P * 100)}%, dropping {sorted(_BB_DROP_KINDS)})")

except Exception as exc:
    print(f"NOTE: basketball filter is off: {type(exc).__name__}: {exc}")

print("OK: ticket_ask.py loaded (basketball: count first, then build to your odds)")
