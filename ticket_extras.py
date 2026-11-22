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

It also adds the pick "No goal scored by a substitute" (SportyBet's "Goal Scored by a Substitution"
market, answer No). Ask for it with the word "substitute" or "substitution", for example
"goal scored by a substitution no 10 picks tomorrow". It is also offered in normal tickets.

Load it AFTER upgrades.py. In app.py it goes into the same block as push_notify:

    try:
        import ticket_extras
    except Exception as exc:
        print(f"NOTE: ticket extras are off: {type(exc).__name__}: {exc}")
"""

import os
import re
import threading

import main as bot
import smart_ticket as st

try:
    import upgrades as up
except Exception:  # upgrades.py is optional
    up = None

# Basketball has no football stats: its picks come from SportyBet's prices.
# A basketball-only ticket needs at least this chance per pick (the football bar is 0.66).
BB_MIN_P = float(os.getenv("BB_MIN_P", "0.52"))

# "over 1.5 acca today": no size given, so this many games (upgrades.py would make it 5).
ACCA_DEFAULT_PICKS = int(os.getenv("ACCA_DEFAULT_PICKS", "10"))

LOADED = True  # smart_ticket.py checks this when it explains a basketball problem

_LOCAL = threading.local()  # the message exactly as the person typed it
_HAS_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:odds?|picks?|games?|matches|selections?|tips?|legs?|teams?)\b",
    re.I,
)


# ------------------------------------------------------------
# 1) basketball picks pass upgrades.py's "allowed pick types" check
# ------------------------------------------------------------
_prev_allowed = st._candidate_is_allowed


def _candidate_is_allowed(kind, label=""):

    if str(kind or "").startswith("bb_") or kind == "sub_goal":
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

        if getattr(_SUB_FLAG, "only", False):
            limit = max(int(limit or 0), SUB_DETAIL)

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
# 2b) "No goal scored by a substitute"
# ------------------------------------------------------------
# "substitute", "substitution", "substituted", and also "goal scored by a sub"
SUB_RE = re.compile(
    r"\bsubstitut\w*|\bsubs?\b(?=[^.]*\bgoals?\b)|\bgoals?\b[^.]*\bsubs?\b",
    re.I,
)

# A pick needs at least this chance when it is asked for on its own (the normal bar is 0.66).
SUB_MIN_P = float(os.getenv("SUB_MIN_P", "0.50"))

# This market only exists on each game's own page, so a request for it reads more games than usual.
SUB_DETAIL = int(os.getenv("SUB_DETAIL", "150"))

_SUB_STATS = {"events": set()}  # games where the market was found (shown if a ticket cannot be built)

SUB_MIN_ODD = st.MIN_ODD_FLOOR
SUB_MAX_ODD = 10.0

_SUB_BAD_WORDS = ("corner", "card", "booking", "1st", "2nd", "half", "first", "second", "assist", "minute")
_SUB_SEEN = set()
_SUB_FLAG = threading.local()  # True while a request asks for the substitute pick only

st.KIND_BONUS["sub_goal"] = 0.03
st.KIND_CAP["sub_goal"] = 0.25
st.KIND_NAME["sub_goal"] = "No goal by a substitute"


def substitute_candidates(event, markets):
    """SportyBet's "goal scored by a substitute" market, answer No.

    The market is found by its NAME (it has to mention a substitute and a goal), because the
    exact wording is not known. The names seen are printed in the server log."""

    home = str(event.get("homeTeamName", "Home"))
    away = str(event.get("awayTeamName", "Away"))
    found = []

    for market in markets or []:

        name = f"{market.get('desc') or ''} {market.get('name') or ''}".lower().strip()

        if "substitut" not in name:
            continue

        if name not in _SUB_SEEN and len(_SUB_SEEN) < 12:
            _SUB_SEEN.add(name)
            print(f"Substitute market seen: {market.get('id')}: {name}")

        if any(word in name for word in _SUB_BAD_WORDS):
            continue

        if not any(word in name for word in ("goal", "score", "scorer")):
            continue

        live = []

        for outcome in market.get("outcomes", []):

            if outcome.get("isActive") is False:
                continue

            odd = st._f(outcome.get("odds"))

            if odd and odd > 1:
                live.append((outcome, odd))

        if len(live) != 2:
            continue

        answers = {st._label_of(o): (o, odd) for o, odd in live}

        if "yes" not in answers or "no" not in answers:
            continue

        outcome, odd_no = answers["no"]
        odd_yes = answers["yes"][1]

        if not (SUB_MIN_ODD <= odd_no <= SUB_MAX_ODD):
            continue

        chance = (1 / odd_no) / ((1 / odd_no) + (1 / odd_yes))

        h, a = home.lower(), away.lower()
        has_home = "home" in name or (len(h) > 3 and h in name)
        has_away = "away" in name or (len(a) > 3 and a in name)

        team = ""

        if has_home and not has_away:
            team = home
        elif has_away and not has_home:
            team = away

        _SUB_STATS["events"].add(event.get("eventId"))

        found.append({
            "kind": "sub_goal",
            "label": "No goal scored by a substitute" + (f" ({team})" if team else ""),
            "odd": odd_no,
            "p": chance,
            "market_p": chance,  # no match stats exist for this market: the price decides
            "key": (str(market.get("id")), market.get("specifier") or "", str(outcome.get("id"))),
            "extra": True,
        })

    found.sort(key=lambda c: -c["p"])

    return found[:2]


_prev_event_candidates = st.event_candidates


def event_candidates(event, markets):

    if getattr(_SUB_FLAG, "only", False):
        return substitute_candidates(event, markets)

    got = _prev_event_candidates(event, markets)

    try:
        got = got + substitute_candidates(event, markets)
    except Exception as exc:
        print(f"Substitute picks failed: {exc}")

    return got


st.event_candidates = event_candidates

_prev_reason_for = st._reason_for


def _reason_for(candidate):

    if candidate.get("kind") == "sub_goal":
        chance = round(candidate.get("p", 0) * 100)
        return f"SportyBet's prices give 'no goal from a substitute' about {chance}%."

    return _prev_reason_for(candidate)


st._reason_for = _reason_for

SUB_PROMPT = """

SUBSTITUTE PICK
- The bot can use the pick "No goal scored by a substitute". If the user asks for goals scored by a
  substitute / substitution with the answer no, keep the words "substitute no" in "request", for example
  "substitute no 10 picks tomorrow".
"""

# If the AI chat rewrites the request and drops the word "substitute", it is put back.
_prev_ai_dispatch = getattr(bot, "ai_dispatch", None)

if _prev_ai_dispatch is not None:

    def ai_dispatch(chat_id, text, data):

        try:
            if str(data.get("action", "chat")).lower() == "ticket" and SUB_RE.search(str(text or "")):

                request = str(data.get("request", "") or "")

                if not SUB_RE.search(request):
                    data = dict(data)
                    data["request"] = (request + " substitute no").strip()

        except Exception as exc:
            print(f"Ticket extras: could not keep the substitute request: {exc}")

        return _prev_ai_dispatch(chat_id, text, data)

    bot.ai_dispatch = ai_dispatch

# When a substitute ticket cannot be built, the message says how many games had the market.
_prev_what_text = getattr(st, "_what_text", None)

if _prev_what_text is not None:

    def _what_text(sport_mode, market_only):

        if getattr(_SUB_FLAG, "only", False):
            n = len(_SUB_STATS["events"])
            return f"'No goal scored by a substitute' picks (SportyBet showed that market on {n} of the games I read)"

        return _prev_what_text(sport_mode, market_only)

    st._what_text = _what_text

_prev_prompt = getattr(bot, "ai_system_prompt", None)

if _prev_prompt is not None:

    def ai_system_prompt():
        return _prev_prompt() + SUB_PROMPT

    bot.ai_system_prompt = ai_system_prompt


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

    if SUB_RE.search(text):
        # "goal scored by a substitute no": a "No" pick is rarely 66% likely, so the bar is lowered
        out["STRONG_MIN_P"] = min(out.get("STRONG_MIN_P", getattr(up, "STRONG_MIN_P", SUB_MIN_P)), SUB_MIN_P)

    if market_only is not None:
        # every game may be used (not only top teams), also the tight ones
        out["is_top_league"] = lambda event: True
        out["_is_tight"] = lambda markets: False

        lines = tuple(getattr(up, "NEW_PICK_LINES", ()))

        if not any(abs(float(x) - market_only) < 1e-9 for x in lines):
            out["NEW_PICK_LINES"] = lines + (market_only,)

    return {name: value for name, value in out.items() if hasattr(up, name)}


_prev_handle_text = getattr(bot, "handle_text", None)

if _prev_handle_text is not None:

    def handle_text(chat_id, text):
        _LOCAL.orig = str(text or "")

        try:
            return _prev_handle_text(chat_id, text)
        finally:
            _LOCAL.orig = ""

    bot.handle_text = handle_text

_prev_flow = bot.prediction_ticket_flow


def prediction_ticket_flow(chat_id, text, *args, **kwargs):

    text = text or ""
    changes = {}
    sub_only = False
    cap_before = None

    # decided first, on its own: nothing else that goes wrong may turn it into a normal ticket
    sub_only = bool(SUB_RE.search(text)) and not st.DAILY_RE.search(text.lower())

    if sub_only:
        print(f"Ticket extras: substitute-only request: {text!r}")

    try:
        if not st.DAILY_RE.search(text.lower()):
            changes = _overrides(text)

            # "over 1.5 acca today" (no size): upgrades.py adds "5 picks". Use a longer acca instead.
            market_only, _risk = st._parse_extras(text)
            typed = getattr(_LOCAL, "orig", "")

            if (market_only is not None or sub_only) and typed and not _HAS_NUMBER.search(re.sub(r"\bover\s*[0-4]\.5\b", " ", st._comma_free(typed), flags=re.I)):
                text = re.sub(r"\s+\d+\s*picks?\s*$", "", text, flags=re.I) + f" {ACCA_DEFAULT_PICKS} picks"
    except Exception as exc:
        print(f"Ticket extras: could not read the request: {exc}")

    if sub_only:
        _SUB_FLAG.only = True
        _SUB_STATS["events"].clear()
        cap_before = st.KIND_CAP.get("sub_goal")
        st.KIND_CAP["sub_goal"] = 10.0  # this request is made of this pick only

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

        if sub_only:
            _SUB_FLAG.only = False

            if cap_before is not None:
                st.KIND_CAP["sub_goal"] = cap_before


bot.prediction_ticket_flow = prediction_ticket_flow


# ------------------------------------------------------------
# 5) Home-screen card: "No goal by a substitute"
# ------------------------------------------------------------
# Added to the chat page (app.py's CHAT_EXTRAS). It opens a small window that asks for the odds
# and the days, then sends the request for you, so the words are always exactly right.
CARD_HTML = r"""
<script>
(function () {
  function subRequest(odds, when, risky) {
    var parts = ["goal scored by a substitution no"];
    if (odds) parts.push(odds + " odds");
    if (when) parts.push(when);
    if (risky) parts.push("risky");
    return parts.join(" ");
  }
  window.stSubRequest = subRequest;

  var WHENS = [["tomorrow", "Tomorrow"], ["weekend", "Weekend"], ["today", "Today"], ["this week", "This week"]];
  var state = { odds: "20", when: "tomorrow", risky: false };

  function place() {
    var edit = document.getElementById("editcard");
    if (!edit || document.getElementById("subcard")) return;
    var b = document.createElement("button");
    b.type = "button";
    b.id = "subcard";
    b.className = "card wide redring";
    b.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>' +
      '<div class="wtext"><b>No goal by a substitute</b><span>Goal scored by a substitution: No</span></div>';
    edit.parentNode.insertBefore(b, edit);
    b.addEventListener("click", openSub);
  }

  function openSub() {
    if (typeof accountUser === "undefined" || !accountUser) { showAuth("login"); return; }
    if (typeof canEdit === "function" && !canEdit()) { openTools("premium"); toast("This is a Premium feature."); return; }

    sheet.hidden = false;
    document.getElementById("sheettitle").textContent = "No goal by a substitute";
    pane.scrollTop = 0;

    pane.innerHTML =
      "<h3>How many odds do you want?</h3>" +
      '<p class="hint">I look through SportyBet for the market "Goal Scored by a Substitution" and pick the answer No for each game, until the odds you ask for are reached.</p>' +
      '<label class="field">Total odds<input id="subodds" type="number" inputmode="decimal" min="1.5" max="100000" value="' + state.odds + '" placeholder="e.g. 20"></label>' +
      '<div class="quick" id="subquick">' +
      [10, 20, 50, 100].map(function (v) { return '<button class="mini" type="button" data-so="' + v + '">' + v + " odds</button>"; }).join("") +
      "</div>" +
      '<div class="sect" style="margin-top:6px">Which days?</div>' +
      '<div class="quick" id="subwhen">' +
      WHENS.map(function (w) { return '<button class="mini' + (w[0] === state.when ? " red" : "") + '" type="button" data-sw="' + w[0] + '">' + w[1] + "</button>"; }).join("") +
      "</div>" +
      '<label class="switch"><span><b>Allow weaker picks</b><small>Helps when there are not enough strong games</small></span><input id="subrisk" type="checkbox"' + (state.risky ? " checked" : "") + "></label>" +
      '<p class="msg" id="submsg"></p>' +
      '<button class="btn-block" id="subgo" type="button">Build my ticket</button>' +
      '<p class="hint" style="margin-top:12px">Bigger odds need more games. If there are not enough on SportyBet, I tell you how many I found.</p>';

    var odds = document.getElementById("subodds");
    odds.addEventListener("input", function () { state.odds = odds.value; document.getElementById("submsg").textContent = ""; });
    odds.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); document.getElementById("subgo").click(); } });

    pane.querySelectorAll("[data-so]").forEach(function (b) {
      b.addEventListener("click", function () { odds.value = b.dataset.so; state.odds = odds.value; });
    });
    pane.querySelectorAll("[data-sw]").forEach(function (b) {
      b.addEventListener("click", function () {
        state.when = b.dataset.sw;
        pane.querySelectorAll("[data-sw]").forEach(function (x) { x.classList.toggle("red", x === b); });
      });
    });
    document.getElementById("subrisk").addEventListener("change", function (e) { state.risky = e.target.checked; });

    document.getElementById("subgo").addEventListener("click", function () {
      var n = parseFloat(odds.value);
      if (!(n >= 1.5) || n > 100000) { document.getElementById("submsg").textContent = "Enter odds from 1.5 to 100000."; return; }
      if (typeof busy !== "undefined" && busy) { toast("Wait for your current ticket to finish."); return; }
      closeTools();
      send(subRequest(Number.isInteger(n) ? n : n.toFixed(1), state.when, state.risky));
    });

    setTimeout(function () { odds.focus(); }, 320);
  }

  place();
  var hub = document.getElementById("hub");
  if (hub) new MutationObserver(place).observe(hub, { childList: true, subtree: true });
})();
</script>
"""


def _add_card_to_site():
    """Put the card into app.py's chat page (no change to app.py is needed)."""

    import sys

    for name in ("app", "__main__"):

        module = sys.modules.get(name)

        if module is not None and hasattr(module, "CHAT_EXTRAS"):

            if "id=\"subcard\"" not in module.CHAT_EXTRAS and "stSubRequest" not in module.CHAT_EXTRAS:
                module.CHAT_EXTRAS = module.CHAT_EXTRAS + CARD_HTML

            return True

    return False


try:
    _card_ok = _add_card_to_site()
except Exception as exc:
    _card_ok = False
    print(f"Ticket extras: could not add the home-screen card: {exc}")


print(f"OK: ticket_extras.py loaded (basketball, risky and big odds, substitute pick; home-screen card added: {_card_ok})")
