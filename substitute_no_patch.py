"""Load AFTER upgrades.py and the existing ticket_extras.py.
Keeps the substitute market's No pick eligible in normal mixed tickets without
lowering the confidence threshold for any other market. upgrades.py is untouched.
"""
import os
import upgrades as up
import smart_ticket as st

SUB_MIN_P = float(os.getenv("SUB_MIN_P", "0.50"))

# Enforce No-only for any substitute candidate produced by ticket_extras.
_prev_event_candidates = st.event_candidates
def event_candidates(event, markets):
    candidates = _prev_event_candidates(event, markets)
    return [
        c for c in candidates
        if c.get("kind") != "sub_goal"
        or "no goal scored by a substitute" in str(c.get("label", "")).lower()
    ]
st.event_candidates = event_candidates

# upgrades.py applies STRONG_MIN_P while gathering. Lower it temporarily so the
# sub_goal candidate can survive, then restore the original threshold for all
# other candidate types. The displayed probability remains the real market estimate.
_prev_gather = st.gather
def gather(*args, **kwargs):
    saved = {}
    for attr in ("STRONG_MIN_P", "STRONG_MIN_P_TOP"):
        if hasattr(up, attr):
            saved[attr] = getattr(up, attr)
            setattr(up, attr, min(float(saved[attr]), SUB_MIN_P))
    try:
        result = _prev_gather(*args, **kwargs)
    finally:
        for attr, value in saved.items():
            setattr(up, attr, value)

    try:
        groups = []
        for group in result[0]:
            kept = []
            for candidate in group:
                minimum = SUB_MIN_P if candidate.get("kind") == "sub_goal" else saved.get("STRONG_MIN_P", 0.66)
                if float(candidate.get("p", 0) or 0) >= minimum:
                    kept.append(candidate)
            if kept:
                groups.append(kept)
        return (groups,) + tuple(result[1:])
    except Exception as exc:
        print(f"Substitute No confidence filter warning: {exc}")
        return result
st.gather = gather

print("OK: substitute_no_patch.py loaded â substitute market is No only")
