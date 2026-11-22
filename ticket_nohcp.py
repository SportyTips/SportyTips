"""No handicap picks, no straight win picks (football and basketball), and sure basketball picks only.

Load it AFTER ticket_extras.py (app.py loads it for you):

    football:    "Home +1.5 handicap" and plain "Team to win" picks are never used.
                 (1UP / 2UP, "win either half", team goals, over goals and the rest stay.)
    basketball:  handicap and "Team to win" picks are never used, so only "Over ... points" is left,
                 and a pick needs at least BB_SURE_P (default 62%) chance. The old bar was 52%.

Set NO_WINS=0 on Render to allow straight wins again.
smart_ticket.py, upgrades.py and ticket_extras.py are NOT edited.
Change the bar on Render with BB_SURE_P (for example 0.65 = safer, 0.58 = more games).
"""

import os
import re

import smart_ticket as st

try:
    import ticket_extras as te
except Exception:
    te = None

BB_SURE_P = float(os.getenv("BB_SURE_P", "0.62"))
NO_WINS = os.getenv("NO_WINS", os.getenv("BB_ONLY_POINTS", "1")).strip().lower() in ("1", "true", "yes", "on")

_HCP = re.compile(r"handicap|\bhcp\b", re.I)
_STRAIGHT_WIN = re.compile(r"\bto win\s*$", re.I)  # "Home to win", not "to win (1UP)" or "to win either half"

# 1) football (and every kind a pick can have): no handicap
_prev_allowed = st._candidate_is_allowed


def _candidate_is_allowed(kind, label=""):

    if str(kind or "").lower() in ("handicap", "bb_hcp") or _HCP.search(str(label or "")):
        return False

    if NO_WINS and (str(kind or "").lower() in ("win", "bb_win") or _STRAIGHT_WIN.search(str(label or ""))):
        return False

    return _prev_allowed(kind, label)


st._candidate_is_allowed = _candidate_is_allowed

# 2) basketball picks skip that check, so the word is added to its own list of banned labels
try:
    extra = r"|handicap" + (r"|\bto win\b" if NO_WINS else "")
    st._BB_LABEL_BAD = re.compile(st._BB_LABEL_BAD.pattern + extra, st._BB_LABEL_BAD.flags)
except Exception as exc:
    print(f"No-handicap: could not update the basketball list: {exc}")

# 3) sure basketball picks only (ticket_extras reads this bar for every basketball request)
if te is not None and hasattr(te, "BB_MIN_P"):
    te.BB_MIN_P = max(float(te.BB_MIN_P), BB_SURE_P)

print(f"OK: ticket_nohcp.py loaded (no handicap picks, no straight wins: {NO_WINS}, basketball picks from {round(BB_SURE_P * 100)}%)")
