"""Load AFTER upgrades.py and ticket_extras.py.
Allows only the SportyBet NO substitute-goal outcome into normal mixed tickets.
Other pick kinds retain the original upgrades.py confidence thresholds.
"""
import os
import threading
import smart_ticket as st
import upgrades as up

SUB_MIN_P = float(os.getenv("SUB_MIN_P", "0.50"))
if not 0.0 < SUB_MIN_P < 1.0:
    raise ValueError("SUB_MIN_P must be between 0 and 1")

# ticket_extras.substitute_candidates creates only the NO outcome. Keep its
# candidate intact (especially its SportyBet outcome key) and reject any
# accidental substitute candidate that does not explicitly say NO.
_prev_event_candidates = st.event_candidates
def _no_only_event_candidates(event, markets):
    candidates = _prev_event_candidates(event, markets)
    kept = []
    for c in candidates:
        if str(c.get("kind", "")).lower() != "sub_goal":
            kept.append(c)
            continue
        label = str(c.get("label", "")).lower()
        if "no goal scored by a substitute" in label:
            kept.append(c)
    return kept
st.event_candidates = _no_only_event_candidates

# upgrades.py has two nested gather wrappers. The outermost wrapper does not
# close over _kind_min_p, so patching a closure cell is unreliable. Instead,
# temporarily lower both global floors while gather runs, then restore the
# original floor for every non-substitute candidate in the returned groups.
# A lock prevents another request from observing the temporary global values.
_gather_lock = threading.RLock()
_prev_gather = st.gather

def _gather_with_substitute_no(*args, **kwargs):
    if kwargs.get("daily") or getattr(getattr(up, "CTX", None), "is_daily", False):
        return _prev_gather(*args, **kwargs)

    with _gather_lock:
        old_normal = float(getattr(up, "STRONG_MIN_P", 0.66))
        old_top = float(getattr(up, "STRONG_MIN_P_TOP", old_normal))
        try:
            up.STRONG_MIN_P = min(old_normal, SUB_MIN_P)
            up.STRONG_MIN_P_TOP = min(old_top, SUB_MIN_P)
            result = _prev_gather(*args, **kwargs)
        finally:
            up.STRONG_MIN_P = old_normal
            up.STRONG_MIN_P_TOP = old_top

    # upgrades.gather returns (groups, events, details, studied). Each group
    # is the candidate list for one event. Re-apply the original threshold to
    # every normal pick; only sub_goal gets the dedicated lower threshold.
    if not isinstance(result, tuple) or not result or not isinstance(result[0], list):
        raise RuntimeError("Unexpected smart_ticket.gather result; refusing unsafe filtering")

    groups = []
    for group in result[0]:
        kept = []
        for c in group:
            kind = str(c.get("kind", "")).lower()
            p = float(c.get("p", 0) or 0)
            if kind == "sub_goal":
                if "no goal scored by a substitute" in str(c.get("label", "")).lower() and p >= SUB_MIN_P:
                    kept.append(c)
            else:
                # Preserve the top-league floor where the upgrades context says
                # it applies; current config normally sets both floors equally.
                ctx = getattr(up, "CTX", None)
                floor = old_top if (getattr(ctx, "top_leagues", False) or getattr(ctx, "cups", None)) else old_normal
                if p >= floor:
                    kept.append(c)
        if kept:
            groups.append(kept)
    return (groups,) + tuple(result[1:])

st.gather = _gather_with_substitute_no

print(f"OK: substitute_no_fix loaded; NO-only substitute picks >= {SUB_MIN_P:.0%}; all other confidence floors preserved")
