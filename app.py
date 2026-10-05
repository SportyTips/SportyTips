"""Web front end for SamuelBet AI.

Does NOT edit your bot files. It imports them, swaps the Telegram send
functions for ones that stream to the browser, and calls the bot's own
handle_text() for every message.

Put this file next to main.py, your launcher, sportybet_provider.py,
ticket_image_lite.py and upgrades.py.

Run locally:   python app.py
Production:    gunicorn app:app -w 1 --threads 8 --timeout 300
(keep -w 1: the bot keeps its caches and chat memory in this process)

Env vars: API_FOOTBALL_KEY (required), ANTHROPIC_API_KEY (optional AI chat),
USE_SPORTYBET=1 (needed for the ticket tools). TELEGRAM_BOT_TOKEN is NOT needed.
"""

import base64
import html
import importlib
import json
import os
import queue
import re
import threading
import time

from flask import Flask, Response, jsonify, redirect, request

# Name of your launcher file without .py (the one that starts with
# "Launcher: adds SportyBet-first tickets...").
LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")

RATE_LIMIT = 8        # chat messages allowed per IP...
RATE_WINDOW = 60      # ...per this many seconds
TOOL_LIMIT = 30       # ticket tool clicks allowed per IP per RATE_WINDOW

bot = importlib.import_module("main")
launcher = importlib.import_module(LAUNCHER_MODULE)   # applies its patches to `bot`
try:
    upgrades = importlib.import_module("upgrades")    # 1UP/2UP, human chat, Pidgin
except ImportError:
    upgrades = None
try:
    football_data = importlib.import_module("football_data")   # last 5 games + head-to-head
except ImportError:
    football_data = None
try:
    smart_ticket = importlib.import_module("smart_ticket")     # ticket builder
except ImportError:
    smart_ticket = None

# ---------------------------------------------------------------
# Replace Telegram output with a per-request queue
# ---------------------------------------------------------------
_ctx = threading.local()


def _emit(item):
    q = getattr(_ctx, "q", None)
    if q is not None:
        q.put(item)


# Lines containing these are API error / quota notes. They are hidden on the site.
HIDE_LINES = re.compile(
    r"API-Football|API limit|API problem|API requests|Daily API|"
    r"Booking code failed|SportyBet: |plan only allows",
    re.I,
)


def web_send_message(chat_id, text):
    kept = [line for line in text.split("\n") if not HIDE_LINES.search(line)]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if not cleaned:
        cleaned = "I couldn't get that right now. Please try again in a minute."
    _emit({"type": "text", "html": cleaned})


def web_send_photo(chat_id, png, caption=""):
    _emit({"type": "image", "data": base64.b64encode(png).decode("ascii"),
           "caption": caption})


bot.send_message = web_send_message
bot.telegram_request = lambda method, params=None: {"ok": True, "result": []}
launcher.send_photo = web_send_photo

# ---------------------------------------------------------------
# Server
# ---------------------------------------------------------------
app = Flask(__name__)
RUN_LOCK = threading.Lock()      # the bot's globals are not thread-safe
_hits = {}


def too_fast(key, limit=RATE_LIMIT):
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]
    if len(recent) >= limit:
        _hits[key] = recent
        return True
    recent.append(now)
    _hits[key] = recent
    return False


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


LOCK_WAIT_SECONDS = 240      # how long a message waits behind another ticket before giving up


def with_lock(q, work):
    """Only one ticket is built at a time. If another is running, SAY so instead of loading in silence."""
    got_lock = RUN_LOCK.acquire(blocking=False)
    try:
        if not got_lock:
            q.put({"type": "text", "html": "⏳ I'm still finishing another ticket. Yours is next, one moment..."})
            got_lock = RUN_LOCK.acquire(timeout=LOCK_WAIT_SECONDS)
            if not got_lock:
                q.put({"type": "text", "html": "I'm still busy with another ticket. Please try again in a minute."})
                return
        work()
    except Exception as exc:
        q.put({"type": "text", "html": "❌ " + html.escape(str(exc)[:200])})
    finally:
        if got_lock:
            RUN_LOCK.release()
        q.put(None)


def run_turn(session, text, q, pidgin=False):
    _ctx.q = q
    if upgrades is not None:
        upgrades.CTX.pidgin = pidgin
    with_lock(q, lambda: bot.handle_text(session, text))


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NO_CACHE = {"Cache-Control": "no-cache"}


def _read(name):
    path = os.path.join(BASE_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _pages():
    """(home_page, chat_page). The chat page is the file that has the message box."""
    pages = [p for p in (_read("home.html"), _read("index.html")) if p]
    chat = next((p for p in pages if 'id="messages"' in p), None)
    home = next((p for p in pages if 'id="messages"' not in p), None)
    return home, chat


@app.get("/")
def index():
    home, chat = _pages()
    if home is None:
        return redirect("/chat")          # no separate home page: go straight to the chat
    return Response(home, mimetype="text/html", headers=NO_CACHE)


@app.get("/index.html")
def chat_alias():
    return redirect("/chat")


@app.get("/chat")
def chat_page():
    home, chat = _pages()
    if chat is None:
        return "The chat page is missing. It is the file that has the message box.", 404
    return Response(chat, mimetype="text/html", headers=NO_CACHE)


@app.get("/status")
def status():
    """Open this while the chat is loading: 'busy' means a ticket is still being built."""
    return "busy" if RUN_LOCK.locked() else "free"


@app.get("/health")
def health():
    return "ok"


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()[:500]
    session = str(data.get("session", ""))[:64]
    pidgin = bool(data.get("pidgin"))
    if not text or not session:
        return jsonify(error="Empty message."), 400

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    q = queue.Queue()
    threading.Thread(target=run_turn, args=(session, text, q, pidgin), daemon=True).start()
    return stream_response(q)


def stream_response(q):
    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield "\n"            # keep-alive while the bot waits on API limits
                continue
            if item is None:
                break
            yield json.dumps(item) + "\n"

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def run_build(session, spec, q, pidgin=False):
    _ctx.q = q
    if upgrades is not None:
        upgrades.CTX.pidgin = pidgin
    with_lock(q, lambda: smart_ticket.flow_spec("web-" + session, spec))


@app.post("/api/build")
def build():
    """The ticket builder card: market, side, games, odds and day chosen by the user."""
    data = request.get_json(silent=True) or {}
    session = str(data.get("session", ""))[:64]
    if not session:
        return jsonify(error="Empty request."), 400
    if smart_ticket is None:
        return jsonify(error="The ticket builder is not installed on the server."), 503
    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    def number(key, low, high, whole=False):
        try:
            value = float(data.get(key))
        except (TypeError, ValueError):
            return None
        if not (low <= value <= high):
            return None
        return int(value) if whole else value

    spec = {
        "mode": data.get("mode") if data.get("mode") in ("up2", "up1", "win", "dc", "mixed") else "up2",
        "side": data.get("side") if data.get("side") in ("home", "away", "any") else "any",
        "window": data.get("window") if data.get("window") in ("today", "tonight", "tomorrow") else "today",
        "games": number("games", 1, 30, whole=True),
        "odds": number("odds", 1.01, 100000),
        "risk": "safe",
    }
    q = queue.Queue()
    threading.Thread(target=run_build, args=(session, spec, q, bool(data.get("pidgin"))), daemon=True).start()
    return stream_response(q)


# ---------------------------------------------------------------
# Ticket tools (check / edit / make safer a SportyBet code)
# ---------------------------------------------------------------
CODE_OK = re.compile(r"^[A-Z0-9]{5,12}$")


def _ticket_tool(work):
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()
    if not CODE_OK.match(code):
        return jsonify(error="That does not look like a SportyBet code."), 400
    if too_fast("tool:" + client_ip(), TOOL_LIMIT):
        return jsonify(error="Slow down. Try again in a minute."), 429
    provider = getattr(bot, "SPORTYBET_PROVIDER", None)
    if provider is None:
        return jsonify(error="SportyBet mode is off. Set USE_SPORTYBET to 1 on the server."), 503
    try:
        return jsonify(work(provider, code, data))
    except Exception as exc:
        print(f"Ticket tool error: {exc}")
        return jsonify(error=f"I could not read SportyBet right now: {exc}"), 502


def _ints(value):
    return [int(x) for x in (value or []) if str(x).isdigit()][:30]


@app.post("/api/check")
def api_check():
    return _ticket_tool(lambda p, code, data: p.check_code(code))


@app.post("/api/edit")
def api_edit():
    picker = football_data.safest_for_leg if football_data else None
    return _ticket_tool(lambda p, code, data: p.edit_code(
        code, remove=_ints(data.get("remove")), swap=_ints(data.get("swap")),
        picker=(lambda leg: picker(p, leg)) if picker else None))


@app.post("/api/safer")
def api_safer():
    def work(provider, code, data):
        if football_data:
            return football_data.rebuild_safer(provider, code)
        result = provider.make_safer(code)
        new_code = result.get("new_code")
        ticket = provider.check_code(new_code or code)
        ticket["changed"] = bool(new_code)
        return ticket
    return _ticket_tool(work)


@app.post("/api/straight_win")
def api_straight_win():
    """Compatibility with older pages: same as typing 'straight win today'."""
    data = request.get_json(silent=True) or {}
    session = str(data.get("session", ""))[:64]
    if not session:
        return jsonify(error="Missing session."), 400
    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429
    text = "straight win long ticket" if str(data.get("window", "today")).lower() == "long" else "straight win today"
    q = queue.Queue()
    threading.Thread(target=run_turn, args=(session, text, q), daemon=True).start()
    return stream_response(q)


@app.post("/api/why")
def api_why():
    def work(provider, code, data):
        index = data.get("index")
        if not isinstance(index, int):
            raise ValueError("Missing pick number.")
        if football_data is None:
            return {"reason": "Match data is not set up on the server yet.", "has_data": False}
        return football_data.why(provider, code, index)
    return _ticket_tool(work)


if __name__ == "__main__":
    if not bot.API_FOOTBALL_KEY:
        print("WARNING: API_FOOTBALL_KEY is missing.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)