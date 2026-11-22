"""Basketball: points only. No handicap, no "to win", and sure picks only.

Football is NOT touched (set FOOTBALL_NO_HCP=1 / FOOTBALL_NO_WINS=1 on Render if you ever want that).

Load it AFTER ticket_extras.py (app.py loads it for you).

A basketball ticket then contains only "Over ... points" picks, each with at least BB_SURE_P
(default 62%) chance. Change it on Render: BB_SURE_P=0.65 is safer, 0.58 gives more games.
BB_NO_WINS=0 lets "to win" picks back in.

smart_ticket.py, upgrades.py and ticket_extras.py are NOT edited.
"""

import os
import re

import smart_ticket as st


def _on(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


BB_SURE_P = float(os.getenv("BB_SURE_P", "0.62"))
BB_NO_WINS = _on("BB_NO_WINS", "1")
BB_NO_HCP = _on("BB_NO_HCP", "1")
FOOTBALL_NO_HCP = _on("FOOTBALL_NO_HCP", "0")
FOOTBALL_NO_WINS = _on("FOOTBALL_NO_WINS", "0")

_BB_DROP_KINDS = set()
if BB_NO_HCP:
    _BB_DROP_KINDS.add("bb_hcp")
if BB_NO_WINS:
    _BB_DROP_KINDS.add("bb_win")

# ------------------------------------------------------------
# basketball: drop the kinds, and keep only sure picks
# ------------------------------------------------------------
_prev_bb = st.basketball_candidates
_COUNT = {"seen": 0, "kept": 0}


def basketball_candidates(event, markets):

    kept = []

    for candidate in _prev_bb(event, markets):

        _COUNT["seen"] += 1

        if candidate.get("kind") in _BB_DROP_KINDS:
            continue

        if candidate.get("p", 0) < BB_SURE_P:
            continue

        _COUNT["kept"] += 1
        kept.append(candidate)

    return kept


st.basketball_candidates = basketball_candidates

# the same words are also banned in the basketball label list, as a second lock
try:
    extra = (r"|handicap" if BB_NO_HCP else "") + (r"|\bto win\b" if BB_NO_WINS else "")
    st._BB_LABEL_BAD = re.compile(st._BB_LABEL_BAD.pattern + extra, st._BB_LABEL_BAD.flags)
except Exception as exc:
    print(f"Basketball filter: could not update the label list: {exc}")

# ------------------------------------------------------------
# football: only when asked for on Render
# ------------------------------------------------------------
if FOOTBALL_NO_HCP or FOOTBALL_NO_WINS:

    _prev_allowed = st._candidate_is_allowed

    def _candidate_is_allowed(kind, label=""):

        kind = str(kind or "").lower()
        label = str(label or "")

        if FOOTBALL_NO_HCP and (kind == "handicap" or re.search(r"handicap", label, re.I)):
            return False

        if FOOTBALL_NO_WINS and (kind == "win" or re.search(r"\bto win\s*$", label, re.I)):
            return False

        return _prev_allowed(kind, label)

    st._candidate_is_allowed = _candidate_is_allowed

print(
    f"OK: ticket_nohcp.py loaded (basketball: points only, picks from {round(BB_SURE_P * 100)}%; "
    f"dropping {sorted(_BB_DROP_KINDS)}; football untouched: {not (FOOTBALL_NO_HCP or FOOTBALL_NO_WINS)})"
)
