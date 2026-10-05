"""SportyTips - football ticket bot.

Everything is driven by SportyBet's own data when USE_SPORTYBET=1.
When SportyBet is off, the bot still runs but has no odds source.
"""

import json
import math
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BRAND = "SportyTips"

# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN") or ""
).strip().strip("\"'")

TELEGRAM_TIMEOUT = 40
MAX_MESSAGE_LENGTH = 3500

LOCAL_TZ = timezone(timedelta(hours=1))
LOCAL_TZ_NAME = "WAT"

MIN_HOURS_BEFORE_KICKOFF = 1
MAX_DAYS_AHEAD = 30

ENABLED_MARKETS = {
    "win",
    "goals",
    "btts",
}

RISK_PROFILES = {
    "safe": {
        "min_prob": 0.65,
        "min_odds": 1.30,
        "overshoot": 1.15,
        "markets": {
            "win",
            "goals",
            "btts",
            "dc",
        },
        "extra_matches": 4,
    },
    "normal": {
        "min_prob": 0.55,
        "min_odds": 1.30,
        "overshoot": 1.35,
        "markets": None,
        "extra_matches": 0,
    },
    "risky": {
        "min_prob": 0.45,
        "min_odds": 1.60,
        "overshoot": 1.60,
        "markets": None,
        "extra_matches": 0,
    },
}


# ============================================================
# SPORTYBET SWITCH
# ============================================================

def _flag(name):
    value = (
        os.getenv(name, "0")
        .strip()
        .strip("\"'")
        .strip()
        .lower()
    )

    return value in (
        "1",
        "true",
        "yes",
        "on",
    )


USE_SPORTYBET = _flag("USE_SPORTYBET")

SPORTYBET_PROVIDER = None
SPORTYBET_ERROR = None

if USE_SPORTYBET:
    try:
        from sportybet_provider import SportyBetProvider

        SPORTYBET_PROVIDER = SportyBetProvider()

    except Exception as exc:
        SPORTYBET_ERROR = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "SportyBet provider failed to load:",
            SPORTYBET_ERROR,
        )

        traceback.print_exc()

else:
    print(
        "SportyBet is OFF: "
        "USE_SPORTYBET is not set to 1 "
        "in this process."
    )


def sportybet_off_reason():
    """Plain-text reason why SPORTYBET_PROVIDER is None."""

    if not USE_SPORTYBET:
        return (
            "USE_SPORTYBET is not set to 1 "
            "on this server."
        )

    if SPORTYBET_ERROR:
        return (
            "Provider failed to load: "
            f"{SPORTYBET_ERROR}"
        )

    return "Provider is not available."


class BotError(Exception):
    pass


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(
    method,
    params=None,
):
    if not BOT_TOKEN:
        raise BotError(
            "TELEGRAM_BOT_TOKEN is missing."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    data = (
        urlencode(params).encode("utf-8")
        if params
        else None
    )

    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
    )

    try:
        with urlopen(
            request,
            timeout=TELEGRAM_TIMEOUT,
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise BotError(
            f"Telegram HTTP error "
            f"{exc.code}: {body}"
        )

    except URLError as exc:

        raise BotError(
            f"Telegram connection error: {exc}"
        )

    if not result.get("ok"):
        raise BotError(
            f"Telegram API error: {result}"
        )

    return result


def split_text(
    text,
    limit=MAX_MESSAGE_LENGTH,
):
    chunks = []
    current = ""

    for line in text.split("\n"):

        if (
            len(current)
            + len(line)
            + 1
            > limit
        ):
            if current.strip():
                chunks.append(
                    current.rstrip()
                )

            current = ""

        current += line + "\n"

    if current.strip():
        chunks.append(
            current.rstrip()
        )

    return chunks


def send_message(
    chat_id,
    text,
):
    for chunk in split_text(text):

        telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview":
                    "true",
            },
        )


# ============================================================
# REQUEST PARSER
# ============================================================

SAFE_WORDS = (
    r"\b("
    r"safe|safer|safest|secure|"
    r"low[- ]risk|bankers?"
    r")\b"
)

RISKY_WORDS = (
    r"\b("
    r"risky|riskier|riskiest|"
    r"high[- ]risk|bold|aggressive|"
    r"long ?shots?"
    r")\b"
)


# Recognizes:
#   straight win
#   straight winning
#   straight wins
STRAIGHT_WIN_RE = re.compile(
    r"\bstraight\s*-?\s*(?:win|winning|wins)\b",
    re.I,
)

STRAIGHT_LONG_RE = re.compile(
    r"\b(?:long|longer|extended)\b",
    re.I,
)


def parse_request(text):
    """
    Understand natural-language betting requests.

    Examples:

        safe 5 odds
        today's 10 odds
        give me 20 odds
        20 odds today
        20 odds tomorrow
        safe 30 odds
        5 picks today
        straight win 30 odds
        straight winning 20 odds tomorrow
    """

    message = (
        text or ""
    ).strip().lower()

    now = datetime.now(
        timezone.utc
    )

    local_now = now.astimezone(
        LOCAL_TZ
    )

    local_midnight = (
        local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    )

    # --------------------------------------------------------
    # DEFAULT WINDOW
    #
    # If the user does NOT specify a date:
    #
    # "20 odds"
    # "safe 5 odds"
    #
    # start with the next available period.
    # smart_ticket.py can extend the search window when
    # necessary to reach the requested combined odds.
    # --------------------------------------------------------

    start = (
        now
        + timedelta(
            hours=MIN_HOURS_BEFORE_KICKOFF
        )
    )

    end = (
        now
        + timedelta(
            days=1
        )
    )

    label = "next 24 hours"

    # --------------------------------------------------------
    # DATE / WINDOW DETECTION
    # --------------------------------------------------------

    days_match = re.search(
        r"\b(?:next\s+)?(\d+)\s*days?\b",
        message,
    )

    if days_match:

        days = max(
            1,
            min(
                int(days_match.group(1)),
                MAX_DAYS_AHEAD,
            ),
        )

        end = (
            now
            + timedelta(
                days=days
            )
        )

        label = (
            f"next {days} day"
            + (
                "s"
                if days != 1
                else ""
            )
        )

    elif re.search(
        r"\bweekend\b",
        message,
    ):

        weekday = (
            local_now.weekday()
        )

        if weekday == 6:

            window_start = (
                local_midnight
            )

            window_days = 1

        else:

            window_start = (
                local_midnight
                + timedelta(
                    days=(
                        5 - weekday
                    ) % 7
                )
            )

            window_days = 2

        start = max(
            start,
            window_start,
        )

        end = (
            window_start
            + timedelta(
                days=window_days
            )
        )

        label = "this weekend"

    elif re.search(
        r"\btomorrow\b",
        message,
    ):

        window_start = (
            local_midnight
            + timedelta(
                days=1
            )
        )

        start = max(
            start,
            window_start,
        )

        end = (
            window_start
            + timedelta(
                days=1
            )
        )

        label = "tomorrow"

    elif re.search(
        r"\btonight\b",
        message,
    ):

        window_start = (
            local_midnight
            + timedelta(
                hours=17
            )
        )

        start = max(
            start,
            window_start,
        )

        end = (
            local_midnight
            + timedelta(
                days=1,
                hours=3,
            )
        )

        label = "tonight"

    elif re.search(
        r"\btoday\b",
        message,
    ):

        end = (
            local_midnight
            + timedelta(
                days=1
            )
        )

        label = "today"

    elif re.search(
        r"\bweek\b",
        message,
    ):

        end = (
            now
            + timedelta(
                days=7
            )
        )

        label = "next 7 days"

    # --------------------------------------------------------
    # TARGET ODDS
    #
    # Understands:
    #
    # 5 odds
    # 10 odds
    # 20 odds
    # 30 total odds
    # odds 20
    # odds of 20
    # odds = 20
    #
    # IMPORTANT:
    # This means COMBINED ODDS, not number of picks.
    # --------------------------------------------------------

    target_odds = None

    odds_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*"
        r"(?:total\s+)?odds?\b",
        message,
    )

    if not odds_match:

        odds_match = re.search(
            r"\bodds?\s*"
            r"(?:of|:|=)?\s*"
            r"(\d+(?:\.\d+)?)",
            message,
        )

    if odds_match:

        value = float(
            odds_match.group(1)
        )

        if (
            1.01
            <= value
            <= 100000
        ):
            target_odds = value

    # --------------------------------------------------------
    # NUMBER OF PICKS
    #
    # Only interpret a number as picks when the user actually
    # says picks/games/matches/selections/etc.
    #
    # Therefore:
    #
    # "5 odds"   -> 5.00 combined odds
    # "5 picks"  -> 5 selections
    # --------------------------------------------------------

    picks = None

    picks_match = re.search(
        r"\b(\d+)\s*"
        r"(?:picks?|games?|matches|"
        r"match|selections?|"
        r"predictions?|tips?|"
        r"legs?|teams?)\b",
        message,
    )

    if picks_match:

        value = int(
            picks_match.group(1)
        )

        if (
            1
            <= value
            <= 60
        ):
            picks = value

    # --------------------------------------------------------
    # RISK
    # --------------------------------------------------------

    risk = "normal"

    if re.search(
        SAFE_WORDS,
        message,
    ):
        risk = "safe"

    elif re.search(
        RISKY_WORDS,
        message,
    ):
        risk = "risky"

    return {
        "start": start,
        "end": end,
        "label": label,
        "target_odds": target_odds,
        "picks": picks,
        "risk": risk,
    }


def format_odds(value):

    return (
        str(int(value))
        if value == int(value)
        else str(value)
    )


def describe_request(req):

    odds = (
        format_odds(
            req["target_odds"]
        )
        if req["target_odds"]
        else "not set"
    )

    picks = (
        str(req["picks"])
        if req["picks"]
        else "auto"
    )

    risk = {
        "safe": "🛡️ Safe",
        "risky": "🔥 Risky",
    }.get(
        req.get("risk"),
        "Normal",
    )

    return (
        f"📅 Window: "
        f"{escape(req['label'])}\n"
        f"🎯 Target odds: {odds}\n"
        f"🔢 Picks: {picks}\n"
        f"⚖️ Risk: {risk}"
    )


# ============================================================
# REAL TICKET BUILDER
# ============================================================

def prediction_ticket_flow(
    chat_id,
    text,
    search_days=None,
    target_override=None,
    count_override=None,
):
    """Run the real SportyTips ticket builder."""

    if SPORTYBET_PROVIDER is None:

        send_message(
            chat_id,
            (
                "❌ SportyBet mode is off. "
                f"{escape(sportybet_off_reason())}"
            ),
        )

        return

    try:

        import smart_ticket

        smart_ticket.flow(
            chat_id,
            text,
            search_days=search_days,
            target_override=target_override,
            count_override=count_override,
        )

    except Exception as exc:

        traceback.print_exc()

        send_message(
            chat_id,
            (
                "❌ Ticket builder error:\n"
                f"{escape(type(exc).__name__)}: "
                f"{escape(str(exc))}"
            ),
        )


# ============================================================
# LEGACY STUBS
# ============================================================

def build_options(
    *args,
    **kwargs,
):
    return []


def filter_available(
    fixture,
    options,
    errors,
    min_odds=None,
):
    return options


def ai_system_prompt(
    *args,
    **kwargs,
):
    return ""


# ============================================================
# /markets COMMAND
# ============================================================

def markets_message(
    event_id,
):

    if SPORTYBET_PROVIDER is None:

        return (
            "❌ SportyBet mode is off. "
            f"{escape(sportybet_off_reason())}"
        )

    try:

        markets = (
            SPORTYBET_PROVIDER
            .get_event_markets(
                event_id
            )
        )

    except Exception as exc:

        return (
            "❌ Could not read that event: "
            f"{escape(str(exc))}"
        )

    if not markets:

        return (
            "❌ No markets returned for "
            f"<code>{escape(event_id)}</code>."
        )

    lines = [
        (
            f"📋 <b>Markets for</b> "
            f"<code>{escape(event_id)}</code>"
        ),
        "",
    ]

    seen = set()

    for market in markets:

        mid = market.get("id")

        spec = (
            market.get(
                "specifier"
            )
            or ""
        )

        desc = (
            market.get("desc")
            or market.get("name")
            or ""
        )

        key = (
            mid,
            spec,
            desc,
        )

        if key in seen:
            continue

        seen.add(key)

        lines.append(
            f"<code>{escape(str(mid))}</code> "
            f"| {escape(spec)} "
            f"| {escape(desc)}"
        )

    return "\n".join(
        lines[:80]
    )


# ============================================================
# STRAIGHT-WIN STATE
# ============================================================

_pending = {}


def _is_cancel(
    text,
):

    return (
        text.strip().lower()
        in (
            "cancel",
            "stop",
            "never mind",
            "forget it",
        )
    )


def _run_ticket(
    chat_id,
    req_text,
):
    """
    Send the user's complete natural-language request
    to smart_ticket.py.

    This preserves:
        target odds
        number of picks
        date
        risk level
        straight-win mode
    """

    try:

        req = parse_request(
            req_text
        )

        # ----------------------------------------------------
        # Convert recognised date labels into the exact
        # search_days values supported by smart_ticket.py.
        # ----------------------------------------------------

        search_days = None

        if req["label"] == "today":

            search_days = 1

        elif req["label"] == "tomorrow":

            search_days = 2

        elif req["label"] == "next 7 days":

            search_days = 7

        elif req["label"] == "this weekend":

            search_days = 3

        elif req["label"].startswith(
            "next "
        ):

            match = re.search(
                r"next\s+(\d+)\s+days?",
                req["label"],
            )

            if match:

                days = int(
                    match.group(1)
                )

                # smart_ticket.py supports:
                # 1, 2, 3, 5, 7, 14
                supported = (
                    1,
                    2,
                    3,
                    5,
                    7,
                    14,
                )

                if days in supported:

                    search_days = days

                elif days < 5:

                    search_days = 3

                elif days < 7:

                    search_days = 5

                elif days < 14:

                    search_days = 7

                else:

                    search_days = 14

        prediction_ticket_flow(
            chat_id,
            req_text,
            search_days=search_days,
            target_override=req[
                "target_odds"
            ],
            count_override=req[
                "picks"
            ],
        )

    except Exception as exc:

        traceback.print_exc()

        send_message(
            chat_id,
            (
                "❌ Error:\n"
                f"{escape(type(exc).__name__)}: "
                f"{escape(str(exc))}"
            ),
        )


def _handle_pending(
    chat_id,
    text,
):

    state = _pending.get(
        chat_id
    )

    if state is None:
        return False

    if _is_cancel(text):

        _pending.pop(
            chat_id,
            None,
        )

        send_message(
            chat_id,
            "OK, cancelled.",
        )

        return True

    if state["stage"] == "odds":

        m = re.search(
            r"\d+(?:\.\d+)?",
            text.replace(
                ",",
                "",
            ),
        )

        if not m:

            send_message(
                chat_id,
                (
                    "What total odds? "
                    "Give me a number "
                    "(e.g. 20, 300)."
                ),
            )

            return True

        odds = float(
            m.group()
        )

        if not (
            1.5
            <= odds
            <= 100000
        ):

            send_message(
                chat_id,
                (
                    "Give me odds "
                    "between 1.5 and 100000."
                ),
            )

            return True

        window = state.get(
            "window",
            "today",
        )

        _pending.pop(
            chat_id,
            None,
        )

        tag = (
            "straight win long ticket"
            if window == "long"
            else "straight win today"
        )

        _run_ticket(
            chat_id,
            (
                f"{tag} "
                f"{format_odds(odds)} odds"
            ),
        )

        return True

    return False


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_text(
    chat_id,
    text,
):

    message = (
        text or ""
    ).strip()

    lowered = message.lower()

    if lowered.startswith("/"):

        parts = lowered.split(
            None,
            1,
        )

        command = (
            parts[0]
            .split("@")[0]
        )

        args = (
            parts[1]
            if len(parts) > 1
            else ""
        )

    else:

        command = ""
        args = ""

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if command == "/start":

        send_message(
            chat_id,
            START_TEXT,
        )

        return

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if command == "/help":

        send_message(
            chat_id,
            HELP_TEXT,
        )

        return

    # --------------------------------------------------------
    # RESET
    # --------------------------------------------------------

    if command == "/reset":

        _pending.pop(
            chat_id,
            None,
        )

        send_message(
            chat_id,
            "🧹 Cleared.",
        )

        return

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if command == "/status":

        if SPORTYBET_PROVIDER is not None:

            send_message(
                chat_id,
                "✅ SportyBet mode is ON.",
            )

        else:

            send_message(
                chat_id,
                (
                    "❌ SportyBet mode is off. "
                    f"{escape(sportybet_off_reason())}"
                ),
            )

        return

    # --------------------------------------------------------
    # MARKETS
    # --------------------------------------------------------

    if command == "/markets":

        if not args.strip():

            send_message(
                chat_id,
                (
                    "Usage: "
                    "/markets sr:match:12345678"
                ),
            )

            return

        send_message(
            chat_id,
            markets_message(
                args.strip()
            ),
        )

        return

    # --------------------------------------------------------
    # NORMAL TICKET COMMAND
    # --------------------------------------------------------

    if command == "/ticket":

        _run_ticket(
            chat_id,
            (
                args
                if args
                else "5 picks today"
            ),
        )

        return

    # --------------------------------------------------------
    # PENDING STATE
    # --------------------------------------------------------

    if not command and _handle_pending(
        chat_id,
        message,
    ):

        return

    # ========================================================
    # STRAIGHT WIN / STRAIGHT WINNING
    #
    # IMPORTANT:
    # Send the user's ORIGINAL message.
    #
    # This means:
    #
    # straight win 30 odds
    #
    # stays:
    #
    # straight win 30 odds
    #
    # instead of becoming only:
    #
    # straight win today
    #
    # smart_ticket.py will then detect the 30 odds.
    # ========================================================

    if (
        not command
        and STRAIGHT_WIN_RE.search(
            message
        )
    ):

        _run_ticket(
            chat_id,
            message,
        )

        return

    # ========================================================
    # SMALL TALK
    # ========================================================

    reply = smalltalk_reply(
        lowered
    )

    if reply:

        send_message(
            chat_id,
            escape(
                reply,
                quote=False,
            ),
        )

        return

    # ========================================================
    # NORMAL REQUEST
    # ========================================================

    if (
        not command
        and looks_like_request(
            lowered
        )
    ):

        _run_ticket(
            chat_id,
            message,
        )

        return

    # ========================================================
    # UNKNOWN
    # ========================================================

    send_message(
        chat_id,
        UNKNOWN_TEXT,
    )


# ============================================================
# KEYWORD-ONLY BRAIN
# ============================================================

PREDICTION_WORDS = [
    "prediction",
    "predict",
    "bet",
    "ticket",
    "odds",
    "acca",
    "accumulator",
    "tonight",
    "today",
    "tomorrow",
    "weekend",
    "match",
    "matches",
    "game",
    "games",
    "pick",
    "picks",
    "tip",
    "tips",
    "day",
]


def looks_like_request(
    text,
):

    return any(
        re.search(
            r"\b"
            + re.escape(word)
            + r"s?\b",
            text,
        )
        for word in PREDICTION_WORDS
    )


# ============================================================
# SMALL TALK
# ============================================================

SMALLTALK = [
    (
        r"\b("
        r"how are you|how r u|"
        r"how far|what'?s up|"
        r"whats up|sup"
        r")\b",
        "I'm good. Ready when you are.",
    ),
    (
        r"^\s*("
        r"hi|hello|hey|hiya|yo|"
        r"good (morning|afternoon|evening)|"
        r"howdy"
        r")\b",
        (
            "Hello! Ask me for a ticket, "
            "or paste a SportyBet code."
        ),
    ),
    (
        r"\b("
        r"thanks|thank you|thx|"
        r"nice one|well done"
        r")\b",
        "You're welcome.",
    ),
    (
        r"\b("
        r"who are you|what can you do|"
        r"what do you do|your name"
        r")\b",
        (
            "I'm SportyTips. I build football "
            "tickets from SportyBet's own "
            "matches and odds."
        ),
    ),
]


def smalltalk_reply(
    text,
):

    for pattern, reply in SMALLTALK:

        if re.search(
            pattern,
            text,
        ):

            return reply

    return None


# ============================================================
# TEXTS
# ============================================================

START_TEXT = (
    f"⚽ <b>Welcome to {BRAND}!</b>\n\n"
    "Ask me naturally. You don't need to use "
    "a special format.\n\n"
    "<b>Examples:</b>\n"
    "• safe 5 odds\n"
    "• today's 10 odds\n"
    "• give me 20 odds\n"
    "• 20 odds tomorrow\n"
    "• straight win 30 odds\n\n"
    "<b>Commands:</b>\n"
    "/status — check SportyBet mode\n"
    "/markets sr:match:12345678 — inspect a match\n"
    "/reset — clear state"
)


HELP_TEXT = (
    f"⚽ <b>{BRAND} Help</b>\n\n"
    "<b>Normal ticket:</b>\n"
    "• safe 5 odds\n"
    "• today's 10 odds\n"
    "• give me 20 odds\n"
    "• safe 30 odds tomorrow\n"
    "• 5 picks today\n\n"
    "<b>Straight-win mode (1UP / 2UP only):</b>\n"
    "• straight win today\n"
    "• straight win 20 odds\n"
    "• straight win 30 odds tomorrow\n"
    "• straight win long ticket\n\n"
    "<b>Important:</b>\n"
    "• 5 odds means about 5.00 total odds.\n"
    "• 5 picks means 5 selections.\n"
    "• Straight Win uses only 1UP or 2UP.\n"
    "• No 1X2, DNB, handicap, corners, "
    "goals or BTTS in Straight Win mode.\n\n"
    "/status — check SportyBet mode\n"
    "/markets sr:match:12345678 — list markets for a match\n"
    "/reset — clear state"
)


UNKNOWN_TEXT = (
    f"⚽ <b>{BRAND}</b>\n\n"
    "I didn't understand that.\n\n"
    "Try something like:\n"
    "• safe 5 odds\n"
    "• today's 10 odds\n"
    "• give me 20 odds\n"
    "• straight win 30 odds"
)


# ============================================================
# TELEGRAM MAIN LOOP
# ============================================================

def get_updates(
    offset=None,
):

    params = {
        "timeout": 25
    }

    if offset is not None:
        params["offset"] = offset

    result = telegram_request(
        "getUpdates",
        params,
    )

    return result.get(
        "result",
        [],
    )


def main():

    if not BOT_TOKEN:

        print(
            "ERROR: "
            "TELEGRAM_BOT_TOKEN is missing."
        )

        return

    print(
        f"{BRAND} is running. "
        "SportyBet mode: "
        f"{'ON' if SPORTYBET_PROVIDER is not None else 'OFF - ' + sportybet_off_reason()}"
    )

    offset = None

    while True:

        try:

            updates = get_updates(
                offset
            )

            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat_id = (
                    message
                    .get(
                        "chat",
                        {},
                    )
                    .get(
                        "id"
                    )
                )

                text = message.get(
                    "text"
                )

                if not chat_id or not text:
                    continue

                try:

                    handle_text(
                        chat_id,
                        text,
                    )

                except Exception as exc:

                    traceback.print_exc()

                    print(
                        f"Handler error: {exc}"
                    )

        except KeyboardInterrupt:

            print(
                f"{BRAND} stopped."
            )

            break

        except Exception as exc:

            print(
                f"Bot error: {exc}"
            )

            time.sleep(5)


if __name__ == "__main__":
    main()