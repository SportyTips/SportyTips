"""Makes the new ticket options work together with upgrades.py (upgrades.py is NOT edited).

smart_ticket.py now understands
  - big odds ("5000 odds", "16000 odds") and "I don't mind risky legs",
  - "over 2.5 only" / "over 1.5 acca",
  - basketball ("ncaa 100 odds", "safe 10 odds mixed with basketball").

upgrades.py wraps the ticket builder with its own rules, and three of them get in the way:
  1. it only lets pick types it knows through, so every basketball pick was dropped;
  2. it keeps only STRONG picks (66%+), which cannot reach 16,000 odds or a basketball
     ticket, so for those requests the bar is lowered;
  3. "Over 2 / 2.5 goals" is only allowed for top-league games and not in tight games, but when
     you ask for "over 2.5 only" every game may be used.
The settings are changed only while one request runs and are put back afterwards.

Load it AFTER upgrades.py. In app.py it goes into the same block as push_notify:

    try:
        import ticket_extras
    except Exception as exc:
        print(f"NOTE: ticket extras are off: {type(exc).__name__}: {exc}")
"""

import os
import re

import main as bot
import smart_ticket as st

try:
    import upgrades as up
except Exception:  # upgrades.py is optional
    up = None

# Basketball has no football stats: its picks come from SportyBet's prices.
# A basketball-only ticket needs at least this chance per pick (the football bar is 0.66).
BB_MIN_P = float(os.getenv("BB_MIN_P", "0.52"))


# ------------------------------------------------------------
# 1) basketball picks pass upgrades.py's "allowed pick types" check
# ------------------------------------------------------------
_prev_allowed = st._candidate_is_allowed


def _candidate_is_allowed(kind, label=""):

    if str(kind or "").startswith("bb_"):
        # same rule as football: never Under, never cards
        return not re.search(r"\bunder\b|\bcards?\b", str(label or "").lower())

    return _prev_allowed(kind, label)


st._candidate_is_allowed = _candidate_is_allowed


# ------------------------------------------------------------
# 2) upgrades.py's _detail_order takes only the games; give it the new "limit" option
# ------------------------------------------------------------
if hasattr(st, "_detail_order"):

    _prev_detail_order = st._detail_order

    def _detail_order(events, limit=None):

        if not limit:
            return _prev_detail_order(events)

        old = st.MAX_DETAIL_EVENTS
        st.MAX_DETAIL_EVENTS = max(old, int(limit))

        try:
            return _prev_detail_order(events)
        finally:
            st.MAX_DETAIL_EVENTS = old

    st._detail_order = _detail_order


# ------------------------------------------------------------
# 3) per-request settings
# ------------------------------------------------------------
def _overrides(text):
    """The upgrades.py settings to change for this one request."""

    if up is None:
        return {}

    market_only, risk_ok = st._parse_extras(text)
    sport, _league = st._parse_sport(text)

    out = {}

    if risk_ok:
        # "I don't mind risky legs": weaker picks are allowed
        low = min(getattr(st, "RISKY_OK_MIN_P", 0.25), getattr(up, "STRONG_MIN_P", 0.66))
        out["STRONG_MIN_P"] = low
        out["STRONG_MIN_P_TOP"] = min(low, getattr(up, "STRONG_MIN_P_TOP", low))

    elif sport == "basketball":
        out["STRONG_MIN_P"] = min(getattr(up, "STRONG_MIN_P", BB_MIN_P), BB_MIN_P)

    if market_only is not None:
        # every game may be used (not only top teams), also the tight ones
        out["is_top_league"] = lambda event: True
        out["_is_tight"] = lambda markets: False

        lines = tuple(getattr(up, "NEW_PICK_LINES", ()))

        if not any(abs(float(x) - market_only) < 1e-9 for x in lines):
            out["NEW_PICK_LINES"] = lines + (market_only,)

    return {name: value for name, value in out.items() if hasattr(up, name)}


_prev_flow = bot.prediction_ticket_flow


def prediction_ticket_flow(chat_id, text, *args, **kwargs):

    text = text or ""
    changes = {}

    try:
        if not st.DAILY_RE.search(text.lower()):
            changes = _overrides(text)
    except Exception as exc:
        print(f"Ticket extras: could not read the request: {exc}")

    saved = {}

    for name, value in changes.items():
        saved[name] = getattr(up, name)
        setattr(up, name, value)

    if changes:
        print(f"Ticket extras: changed {sorted(changes)} for this request")

    try:
        return _prev_flow(chat_id, text, *args, **kwargs)
    finally:
        for name, value in saved.items():
            setattr(up, name, value)


bot.prediction_ticket_flow = prediction_ticket_flow

print("OK: ticket_extras.py loaded (basketball, risky and big odds work with upgrades.py)")
