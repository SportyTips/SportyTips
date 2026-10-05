"""Upgrades for SportyTips. Does NOT edit main.py.

1) Straight-win mode uses SportyBet's "1X2 - 1UP" or "1X2 - 2UP" market:
   - the bot picks 2UP when it is quite sure, 1UP otherwise
   - if SportyBet does not offer 1UP/2UP for a match AND straight-win mode is on,
     the pick is dropped completely (no fallback to plain win)
2) Straight-win mode is toggled at runtime by smart_ticket.flow()
3) The AI chat is removed entirely -- keyword matching handles messages.
4) No external stats -- every reason comes from SportyBet's own odds.

Put this file next to main.py and import it in app.py AFTER the launcher.
"""

import threading

import main as bot

CTX = threading.local()

USE_UP_MARKETS = True
UP_2UP_MIN_PROB = 0.68       # >= this -> 2UP, otherwise 1UP
STRAIGHT_WIN_ONLY = False    # toggled by smart_ticket.flow()


# ------------------------------------------------------------
# 1) 1UP / 2UP substitution
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
    out = []
    for option in kept:
        spec = option.get("spec") or {}
        # Straight-win mode: drop any win pick that did not resolve to 1UP/2UP
        if STRAIGHT_WIN_ONLY and option.get("kind") == "win":
            if not spec.get("up_used"):
                continue
        if spec.get("up_used") and not option.get("up_applied"):
            n = spec.get("up", 1)
            option["label"] = f"{option['label']} ({n}UP)"
            option["prob"] = max(option["prob"], min(0.95 / option["odd"], 0.97))
            option["up_applied"] = True
        out.append(option)
    return out


bot.build_options = build_options
bot.filter_available = filter_available


# ------------------------------------------------------------
# 2) Human-style AI replies -- REMOVED.
#    No Claude, no Anthropic. Keyword matching in main.py handles everything.
# ------------------------------------------------------------
# (ai_system_prompt is not patched -- main.ai_system_prompt stays as-is but is
#  never called because main.handle_text no longer invokes the AI path.)


# ------------------------------------------------------------
# 3) Load the smart ticket builder (patches bot.prediction_ticket_flow).
# ------------------------------------------------------------
try:
    import smart_ticket  # noqa: F401
except ImportError as exc:
    print(f"smart_ticket.py not found, using the old ticket builder: {exc}")