"""Web frontend for SportyTips.

Does NOT edit your bot files. It imports them, swaps the Telegram send
functions for ones that stream to the browser, and calls the bot's own
handle_text() for every message.

Put this file next to main.py, launcher.py, sportybet_provider.py,
ticket_image_lite.py and upgrades.py.

Run locally:   python app.py
Production:    gunicorn app:app -w 1 --threads 8 --timeout 600
(keep -w 1: the bot keeps its caches and chat memory in this process)

Env vars:
  USE_SPORTYBET=1 (required for tickets)
  TELEGRAM_BOT_TOKEN is NOT needed for the web frontend.
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

from flask import Flask, Response, jsonify, request, send_from_directory

LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")

RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30

bot = importlib.import_module("main")
launcher = importlib.import_module(LAUNCHER_MODULE)
try:
    upgrades = importlib.import_module("upgrades")
except ImportError:
    upgrades = None

# ---------------------------------------------------------------
# Replace Telegram output with a per-request queue
# ---------------------------------------------------------------
_ctx = threading.local()


def _emit(item):
    q = getattr(_ctx, "q", None)
    if q is not None:
        q.put(item)


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
RUN_LOCK = threading.Lock()
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


def run_turn(session, text, q):
    _ctx.q = q
    try:
        with RUN_LOCK:
            bot.handle_text(session, text)
    except Exception as exc:
        q.put({"type": "text", "html": "❌ " + html.escape(str(exc))})
    finally:
        q.put(None)


@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/health")
def health():
    return "ok"


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()[:500]
    session = str(data.get("session", ""))[:64]
    if not text or not session:
        return jsonify(error="Empty message."), 400

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    q = queue.Queue()
    threading.Thread(target=run_turn, args=(session, text, q), daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield "\n"
                continue
            if item is None:
                break
            yield json.dumps(item) + "\n"

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------
# Straight-win shortcuts (used by the two buttons in the web UI)
# ---------------------------------------------------------------
@app.post("/api/straight_win")
def api_straight_win():
    data = request.get_json(silent=True) or {}
    window = str(data.get("window", "today")).lower()
    session = str(data.get("session", ""))[:64]
    if window not in ("today", "long"):
        return jsonify(error="Unknown window."), 400
    if not session:
        return jsonify(error="Missing session."), 400

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    # Inject the trigger message as if the user typed it
    text = "straight win long ticket" if window == "long" else "straight win today"

    q = queue.Queue()
    threading.Thread(target=run_turn, args=(session, text, q), daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield "\n"
                continue
            if item is None:
                break
            yield json.dumps(item) + "\n"

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    return _ticket_tool(lambda p, code, data: p.edit_code(
        code, remove=_ints(data.get("remove")), swap=_ints(data.get("swap")), picker=None))


@app.post("/api/safer")
def api_safer():
    return _ticket_tool(lambda p, code, data: p.make_safer(code))


if __name__ == "__main__":
    if bot.SPORTYBET_PROVIDER is None:
        print("WARNING: USE_SPORTYBET is not set to 1, so tickets cannot be built.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)